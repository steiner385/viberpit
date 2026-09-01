"""Teardown watchdog for TestClient exits (issue #846).

The dominant CI flake is a random server test dying with
``Failed: Timeout (>300.0s)`` in teardown, inside ``TestClient.__exit__`` (app
lifespan shutdown → anyio portal join). pytest-timeout's faulthandler dump is
useless there: it shows thread stacks only — the portal loop "idle in select" —
because the thing that never completes is a COROUTINE, and faulthandler cannot
see coroutine frames.

``TeardownWatchdog`` arms a ``threading.Timer`` around the exit. If teardown
finishes in time the timer is cancelled (zero overhead). If it fires, it dumps
every asyncio task on the app loop — name, repr, and the coroutine await-chain
frames — to the REAL stderr (``sys.__stderr__``, bypassing pytest capture, like
faulthandler does), so the next CI hang names its culprit instead of timing out
mute.

The dump is preferentially executed ON the app loop via
``call_soon_threadsafe`` — in the observed hangs the loop is alive but awaiting
a never-completing coroutine, so the callback runs and the stacks are
consistent. If the loop doesn't run it within a short grace (loop blocked in
sync code), the dump happens cross-thread instead, best-effort.

Wiring: ``server/tests/conftest.py`` wraps ``TestClient.__exit__`` so every
TestClient in the suite is covered. Tune via ``CCSWITCH_TEARDOWN_WATCHDOG_S``
(default 45 — far under pytest-timeout's 300, far over any healthy teardown).
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any, TextIO

from server.aio import format_task_dump

DEFAULT_TIMEOUT_S = 45.0
_ENV_VAR = "CCSWITCH_TEARDOWN_WATCHDOG_S"


class TeardownWatchdog:
    """Context manager: dump the app loop's asyncio tasks if the body overruns.

    Zero-overhead on the happy path (the timer is cancelled on exit). ``out``
    defaults to the real stderr at fire time; tests inject a buffer.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float | None = None,
        label: str = "teardown",
        out: TextIO | None = None,
        loop_dump_grace: float = 5.0,
    ) -> None:
        if timeout is None:
            try:
                timeout = float(os.environ.get(_ENV_VAR, DEFAULT_TIMEOUT_S))
            except ValueError:
                timeout = DEFAULT_TIMEOUT_S
        self._loop = loop
        self._timeout = timeout
        self._label = label
        self._out = out
        self._loop_dump_grace = loop_dump_grace
        self._timer = threading.Timer(timeout, self._fire)
        self._timer.daemon = True
        self._dumped = threading.Event()

    def __enter__(self) -> "TeardownWatchdog":
        self._timer.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._timer.cancel()

    # -- fire path (timer thread) ------------------------------------------
    def _fire(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._dump_on_loop)
        except RuntimeError:
            # Loop already closed — dump whatever the task registry still holds.
            self._dump("loop is CLOSED; cross-thread dump")
            return
        if not self._dumped.wait(self._loop_dump_grace):
            self._dump(
                f"loop did NOT run the dump callback within "
                f"{self._loop_dump_grace:.0f}s — loop blocked in sync code? "
                "cross-thread dump (stacks may be inconsistent)"
            )

    def _dump_on_loop(self) -> None:
        self._dump("dumped ON the app loop — loop is alive; the hang is a "
                   "pending await, not a blocked loop")

    def _dump(self, how: str) -> None:
        if self._dumped.is_set():
            return
        self._dumped.set()
        out = self._out if self._out is not None else sys.__stderr__
        if out is None:  # pragma: no cover — no real stderr to write to
            return
        try:
            tasks = sorted(
                asyncio.all_tasks(self._loop), key=lambda t: t.get_name()
            )
        except Exception as exc:  # pragma: no cover — defensive
            tasks = []
            how += f" (all_tasks failed: {exc!r})"
        current_test = os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>")
        try:
            out.write(
                f"\n===== ccswitch teardown watchdog: {self._label} exceeded "
                f"{self._timeout:.0f}s (issue #846) =====\n"
                f"test: {current_test}\n"
                f"mode: {how}\n"
                f"{len(tasks)} asyncio task(s) on the app loop:\n"
                f"{format_task_dump(tasks)}\n"
                f"===== end teardown watchdog dump =====\n"
            )
            out.flush()
        except Exception:  # pragma: no cover — never let the dump crash teardown
            pass
