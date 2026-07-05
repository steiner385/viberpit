"""Supervise fire-and-forget asyncio tasks so a crash is never silent.

A bare ``asyncio.create_task`` whose coroutine raises drops the exception on the
floor: the only trace is an "exception was never retrieved" warning much later at GC,
and the work the task was doing simply stops. In the transcript pipeline that means a
frozen live tail — no log, no restart, no client signal (audit W2.6).

``supervise`` attaches a done-callback that retrieves the exception (so it is always
observed), logs it loudly with a traceback naming the task, and — for a task that
FAILED rather than was cancelled — invokes an optional recovery hook. Clean returns
and cancellations pass through untouched, so it never changes shutdown behaviour.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


def supervise(
    task: asyncio.Task,
    *,
    name: str,
    on_failure: Callable[[BaseException], None] | None = None,
) -> asyncio.Task:
    """Observe *task*'s outcome via a done-callback; return *task* for chaining.

    - Cancelled  → nothing (deliberate teardown).
    - Clean exit → nothing (exception is None).
    - Failed     → ``log.error(..., exc_info=exc)`` naming *name*, then
      ``on_failure(exc)`` if given. A raise inside ``on_failure`` is itself logged
      and swallowed — a recovery hook must never crash the callback.
    """
    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        log.error("supervised task %s failed", name, exc_info=exc)
        if on_failure is not None:
            try:
                on_failure(exc)
            except Exception:
                log.exception("supervised task %s on_failure hook raised", name)

    task.add_done_callback(_done)
    return task
