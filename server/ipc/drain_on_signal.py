"""Drain in-flight turns on SIGTERM, before the daemon shuts down.

Why this exists (2026-08-30, dobby): every ``systemctl stop/restart
ccswitch-daemon.service`` is the same SIGTERM — the deploy controller, the wedge
watchdog, an operator or an agent typing it after a ``daemon.env`` edit. Only the
deploy controller drained first, over RPC (``server.deploy.drain``), and on a host
whose daemon binds https-only that RPC cannot even connect. So a bare restart killed
five sessions mid-turn. The daemon owns the sessions; it must protect their turns
itself, whoever sent the signal.

How: at lifespan startup the daemon replaces uvicorn's SIGTERM disposition with a loop
handler that

1. enters the orchestrator's drain mode (``begin_drain``: refuse new turns, snapshot
   pending work, tell the UI "update pending"),
2. waits with the SAME ceilings as the deploy drain — ``drain_and_wait`` with its
   ``CCSWITCH_DRAIN_TURN_CAP_SEC`` / ``CCSWITCH_DRAIN_CONDENSE_CAP_SEC`` — while the
   control plane keeps serving (the edge sees a draining daemon, not a dead one), then
3. hands the signal to the handler that was installed before ours (uvicorn's
   ``handle_exit``), which runs the ordinary graceful shutdown: lifespan teardown →
   ``orchestrator.stop()`` → children closed in order.

A second SIGTERM while draining skips the wait (``systemctl kill -s TERM`` is the
operator's escape hatch). The unit's ``TimeoutStopSec`` must cover the condense cap
plus the shutdown budget — see ``deploy/ccswitch-daemon.service`` — and the unit must
use ``KillMode=mixed``: control-group delivery SIGTERMs the claude children in
parallel with us, and there is nothing left to drain.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
import threading
from typing import Any, Awaitable, Callable

from server.deploy.drain import drain_and_wait
from server.ipc.client import DaemonError

log = logging.getLogger("server.ipc.drain_on_signal")

DrainFn = Callable[[Any], Awaitable[str]]


class LocalDrainClient:
    """Adapt the in-process orchestrator to the ``client.call(method)`` surface
    ``drain_and_wait`` polls, so the SIGTERM path reuses the deploy drain verbatim
    (one wait loop, one set of caps) instead of growing a parallel copy."""

    def __init__(self, orchestrator: Any):
        self._orch = orchestrator

    async def call(self, method: str, **params: Any) -> dict:
        if method == "begin_drain":
            result = self._orch.begin_drain()
        elif method == "drain_status":
            result = self._orch.drain_status()
        else:
            raise DaemonError("unsupported_method", f"local drain client has no RPC {method!r}")
        if inspect.isawaitable(result):
            result = await result
        return result


def install_drain_on_sigterm(orchestrator: Any, *, loop: asyncio.AbstractEventLoop | None = None,
                             drain: DrainFn = drain_and_wait,
                             sig: int = signal.SIGTERM) -> Callable[[], None]:
    """Install the drain-then-shutdown SIGTERM handler; return its uninstaller.

    Off the main thread (TestClient-style lifespans) signal handlers cannot be
    installed at all — that degrades to a logged no-op, never an exception, because a
    daemon that fails to start over an optional nicety is worse than one that stops
    the old way.
    """
    if threading.current_thread() is not threading.main_thread():
        log.debug("drain-on-SIGTERM not installed: not the main thread")
        return lambda: None
    loop = loop or asyncio.get_running_loop()
    previous = signal.getsignal(sig)
    state: dict[str, Any] = {"task": None, "handed_off": False}

    def _restore_previous() -> None:
        try:
            loop.remove_signal_handler(sig)
        except Exception:  # noqa: BLE001 — already removed / loop closed
            pass
        # remove_signal_handler leaves SIG_DFL; put back what uvicorn (or the shell)
        # had, so any later signal still takes the ordinary path.
        try:
            signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            log.debug("could not restore previous %s handler", sig, exc_info=True)

    def _hand_off() -> None:
        if state["handed_off"]:
            return
        state["handed_off"] = True
        _restore_previous()
        if callable(previous):
            previous(sig, None)          # uvicorn.Server.handle_exit → graceful shutdown
        else:
            os.kill(os.getpid(), sig)    # nothing to delegate to: take the default action

    async def _drain_then_hand_off() -> None:
        try:
            busy = orchestrator.active_session_count()
            log.info("SIGTERM: draining %d in-flight turn(s) before shutdown", busy)
            result = await drain(LocalDrainClient(orchestrator))
            log.info("SIGTERM drain %s — proceeding to shutdown", result)
        except asyncio.CancelledError:
            return                       # the second-SIGTERM path hands off itself
        except Exception:  # noqa: BLE001 — a broken drain must never block a stop
            log.warning("SIGTERM drain failed — proceeding to shutdown", exc_info=True)
        _hand_off()

    def _on_signal() -> None:
        task = state["task"]
        if task is None:
            state["task"] = loop.create_task(_drain_then_hand_off(), name="drain-on-sigterm")
            return
        log.warning("second SIGTERM while draining — skipping the wait")
        task.cancel()
        _hand_off()

    try:
        loop.add_signal_handler(sig, _on_signal)
    except (RuntimeError, ValueError, NotImplementedError):
        log.warning("drain-on-SIGTERM not installed: loop refused a signal handler", exc_info=True)
        return lambda: None

    def uninstall() -> None:
        if state["handed_off"]:
            return                       # already restored during hand-off
        state["handed_off"] = True
        _restore_previous()

    return uninstall
