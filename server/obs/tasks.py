"""Supervise fire-and-forget asyncio tasks so a crash is never silent.

A bare ``asyncio.create_task`` whose coroutine raises drops the exception on the
floor: the only trace is an "exception was never retrieved" warning much later at GC,
and the work the task was doing simply stops. In the transcript pipeline that means a
frozen live tail — no log, no restart, no client signal (audit W2.6).

``supervise`` attaches a done-callback that retrieves the exception (so it is always
observed), logs it loudly with a traceback naming the task, and — for a task that
FAILED rather than was cancelled — invokes an optional recovery hook. Clean returns
and cancellations pass through untouched, so it never changes shutdown behaviour.

It also holds a strong reference to the task until it finishes. asyncio keeps only
weak references to tasks, so a task parked on a queue whose owner (a torn-down
driver, a closed socket handler) was dropped is itself garbage: the GC logs "Task
was destroyed but it is pending!", closes the coroutine, and the step that close
schedules raises a frame-less ``RuntimeError: cannot reuse already awaited
coroutine`` — reported here as a spurious task failure (#1690).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

#: Strong references to every supervised task still running (see module docstring).
_live: set[asyncio.Task] = set()


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
        _live.discard(t)
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

    _live.add(task)
    task.add_done_callback(_done)
    return task
