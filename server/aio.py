"""Shared asyncio teardown helper.

``bounded_wait`` is the single implementation of "await this, but never hang":
give the awaitable a budget, then cancel it, then give the cancellation a grace
window, then abandon it with a loud log. Shutdown paths must use this instead of
bare ``await`` — an unbounded await in teardown wedged CI's TestClient exits for
11+ minutes until the job's wall-clock timeout killed pytest (issue #846).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger(__name__)

# Default grace given to a cancelled phase to unwind its finally blocks before
# we abandon it. Small on purpose: by the time this applies the phase has
# already blown its primary budget.
_CANCEL_GRACE_S = 2.0


async def bounded_wait(
    aw: Awaitable[Any],
    *,
    timeout: float,
    phase: str,
    cancel_grace: float = _CANCEL_GRACE_S,
) -> bool:
    """Await *aw* for at most ``timeout`` seconds; never hang, never raise.

    On overrun the task is cancelled and given ``cancel_grace`` seconds to
    unwind; if it ignores the cancel too, it is abandoned (CRITICAL log naming
    *phase*) rather than awaited forever — a wedged teardown phase must cost
    seconds, not the process. Exceptions from *aw* are logged, not raised.

    Returns True if the awaitable completed (or failed) on its own, False if it
    had to be cancelled or abandoned.
    """
    task: asyncio.Task[Any] = (
        aw if isinstance(aw, asyncio.Task) else asyncio.ensure_future(aw)
    )
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if done:
        _consume(task, phase)
        return True
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=cancel_grace)
    if done:
        _consume(task, phase)
        return False
    log.critical(
        "bounded_wait: phase %r ignored cancellation %.1fs after a %.1fs budget "
        "— abandoning it (issue #846)", phase, cancel_grace, timeout,
    )
    return False


def _consume(task: asyncio.Task[Any], phase: str) -> None:
    """Retrieve the task's outcome so a failure can't surface as 'exception was
    never retrieved' noise later; log it against the phase name instead."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("bounded_wait: phase %r failed", phase, exc_info=exc)
