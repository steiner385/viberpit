"""Shared asyncio teardown helpers.

``bounded_wait`` is the single implementation of "await this, but never hang":
give the awaitable a budget, then cancel it, then give the cancellation a grace
window, then abandon it with a loud log. Shutdown paths must use this instead of
bare ``await`` — an unbounded await in teardown wedged CI's TestClient exits for
11+ minutes until the job's wall-clock timeout killed pytest (issue #846).

``cancel_and_reap`` is the single implementation of "cancel this child and wait
for it, without swallowing MY OWN cancellation" (issue #1164).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable
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


def _consume(
    task: asyncio.Future[Any], phase: str, *, level: int = logging.WARNING
) -> None:
    """Retrieve the task's outcome so a failure can't surface as 'exception was
    never retrieved' noise later; log it against the phase name instead."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.log(level, "phase %r failed", phase, exc_info=exc)


def _cancel_requested(task: asyncio.Future[Any]) -> bool:
    """True when someone has already asked *task* to cancel.

    A second ``cancel()`` ESCALATES: it lands inside whatever teardown region the
    child entered in response to the first one and cuts that short. Callers that
    cancel before reaping (``CodexRunnerManager.stop`` cancels its supervisor, tears
    the app-server down, then reaps) must still get the child's full unwind.
    ``Task.cancelling()`` (3.11+) is the only way to ask; a plain Future has no
    such state, so it is treated as "not yet asked".
    """
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling()) if callable(cancelling) else False


async def cancel_and_reap(
    task: asyncio.Future[Any] | None,
    *,
    phase: str,
    timeout: float | None = None,
) -> bool:
    """Cancel *task* and wait for it to unwind, swallowing only ITS cancellation.

    The single implementation of the teardown dance that used to be copied into
    every ``stop()``/``aclose()``/detach path (issue #1164). Hand-rolled, it read::

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):   # ← eats the OUTER cancel
            pass

    After ``task.cancel()`` the ``CancelledError`` from ``await task`` is *usually*
    the child's — but when the surrounding coroutine is itself being cancelled (the
    SIGTERM drain, ``server/ipc/drain_on_signal.py``) that handler swallows the outer
    cancellation and the caller runs on as though it were never cancelled. A graceful
    shutdown then overruns ``ccswitch-daemon.service``'s ``TimeoutStopSec`` and is
    SIGKILLed with every live session attached.

    ``asyncio.wait`` makes the distinction STRUCTURAL rather than a heuristic on
    ``Task.cancelling()``: it absorbs the child's cancellation into the task object
    and only ever raises the *caller's* own, so the ambiguity simply does not arise
    and the outer cancellation propagates untouched. This replaces the
    ``is_outer_cancellation()`` check ``server/codex/process.py`` used to define —
    the rule has one implementation, and it lives here.

    *timeout* bounds the wait (None = until the child unwinds); on overrun the child
    is left running rather than awaited forever — a teardown phase must cost seconds,
    not the process (issue #846). Child failures are retrieved and logged at DEBUG:
    these are shutdown paths, and their previous behaviour was to swallow silently.

    Returns True if *task* is finished when we return (including "nothing to reap"),
    False if it outlived the timeout — or if it is the caller's own task, which is
    never cancelled here: self-cancelling a teardown path is the very failure this
    helper exists to prevent.
    """
    if task is None:
        return True
    if task is asyncio.current_task():
        log.warning("cancel_and_reap: phase %r tried to reap its own task — ignored", phase)
        return False
    if not task.done() and not _cancel_requested(task):
        task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if not done:
        log.debug("cancel_and_reap: phase %r still unwinding after %ss — abandoned",
                  phase, timeout)
        return False
    _consume(task, phase, level=logging.DEBUG)
    return True


def iter_task_frames(task: asyncio.Task[Any]) -> tuple[list[Any], Any]:
    """Walk a task's coroutine AWAIT CHAIN; return (frames, terminal awaitable).

    ``Task.get_stack()`` returns only the outermost suspended frame, which for
    any wrapped task (anyio portals, supervisors) names the wrapper, not the
    culprit. This follows ``cr_await``/``gi_yieldfrom``/``ag_await`` down the
    chain, collecting one frame per suspended coroutine, and returns the final
    non-coroutine object being awaited (typically a Future — e.g. the shielded
    aiosqlite close future in the #846 wedge), or None.
    """
    import gc

    frames: list[Any] = []
    obj: Any = None
    try:
        obj = task.get_coro()
        seen: set[int] = set()
        while obj is not None and id(obj) not in seen:
            seen.add(id(obj))
            if hasattr(obj, "cr_frame"):
                frame, nxt = obj.cr_frame, obj.cr_await
            elif hasattr(obj, "gi_frame"):
                frame, nxt = obj.gi_frame, obj.gi_yieldfrom
            elif hasattr(obj, "ag_frame"):
                frame, nxt = obj.ag_frame, obj.ag_await
            elif isinstance(obj, asyncio.Future):
                break  # a Future is a useful terminal — stop here
            else:
                # Wrapper objects hide the next hop behind a C-level reference:
                # `async_generator_asend` (an async-CM __aexit__ in flight)
                # points at its agen, `_asyncio.FutureIter` at its Future.
                # gc.get_referents digs both out; give up if nothing walkable.
                nxt = next(
                    (r for r in gc.get_referents(obj)
                     if hasattr(r, "cr_frame") or hasattr(r, "gi_frame")
                     or hasattr(r, "ag_frame") or isinstance(r, asyncio.Future)),
                    None,
                )
                if nxt is None:
                    break
                obj = nxt
                continue
            if frame is not None:
                frames.append(frame)
            obj = nxt
    except Exception:  # pragma: no cover — diagnostics must never raise
        pass
    return frames, obj


def format_task_dump(tasks: Iterable[asyncio.Task[Any]]) -> str:
    """Render asyncio tasks with their coroutine await chains, one block each.

    faulthandler dumps only THREAD stacks: a loop awaiting a coroutine that never
    completes shows as "idle in select", naming nothing (issue #846). This is the
    coroutine-level complement — task name, repr, every suspended frame in the
    await chain, and the terminal awaitable — used by the teardown watchdog and
    the shutdown reaper.
    """
    lines: list[str] = []
    for i, task in enumerate(tasks):
        try:
            name = task.get_name()
        except Exception:
            name = "<unknown>"
        lines.append(f"--- task {i}: {name} ---")
        try:
            lines.append(f"    {task!r}")
        except Exception:
            lines.append("    <repr failed>")
        frames, terminal = iter_task_frames(task)
        if not frames:
            lines.append("    <no frames — task done or not started>")
        for f in frames:
            code = f.f_code
            lines.append(
                f'    File "{code.co_filename}", line {f.f_lineno}, in {code.co_name}'
            )
        if terminal is not None:
            try:
                lines.append(f"    awaiting: {terminal!r}")
            except Exception:  # pragma: no cover — defensive
                lines.append("    awaiting: <repr failed>")
    return "\n".join(lines)


# Frame-filename markers identifying a task parked inside the async DB stack.
# Matched against every frame in the task's coroutine await chain.
_DB_FRAME_MARKERS = ("sqlalchemy", "aiosqlite")


def _stack_touches(task: asyncio.Task[Any], markers: tuple[str, ...]) -> bool:
    frames, _terminal = iter_task_frames(task)
    for f in frames:
        fn = f.f_code.co_filename
        if any(m in fn for m in markers):
            return True
    return False


async def reap_lingering_db_tasks(
    *,
    rounds: int = 4,
    grace: float = 1.0,
    markers: tuple[str, ...] = _DB_FRAME_MARKERS,
    phase: str = "shutdown:db-task-reap",
) -> list[asyncio.Task[Any]]:
    """Cancel-and-reap tasks still parked inside sqlalchemy/aiosqlite frames.

    Last line of defense for issue #846. By the time this runs (the final
    lifespan-shutdown phase, after dispose_db) any task still inside the async
    DB stack is by definition abandoned — its owner was already stopped or
    bounded_wait gave up on it. Left alone, such a task hangs the EVENT LOOP'S
    OWN CLOSE: asyncio's ``Runner.close``/``_cancel_all_tasks`` delivers exactly
    ONE cancel and then gathers unboundedly, and the DB stack absorbs single
    cancels twice over —

    * SQLAlchemy reacts to a CancelledError mid-query by terminating the
      connection via ``await_(asyncio.shield(_terminate_graceful_close()))``,
      so the cancel is swallowed and the task re-parks on the shield;
    * aiosqlite's ``Connection.close()`` catches a cancel delivered at its
      ``await self._execute(...)`` and then awaits ``stop()``'s future in its
      ``finally`` — forever, if the worker thread already died (it dies when it
      services a future whose loop closed, e.g. a cross-test leak under load).

    That is precisely the TestClient.__exit__ portal-join hang CI kept hitting
    (~300 s pytest-timeout on an unrelated test) and the systemd-stop hang in
    prod. The fix is mechanical: cancel repeatedly — each round peels one
    absorb layer — with a short grace per round, then log a CRITICAL dump of
    anything still alive so the next hang names its culprit.

    Returns the tasks still pending after all rounds (empty = clean).
    """
    me = asyncio.current_task()
    targets: set[asyncio.Task[Any]] = set()
    for attempt in range(rounds):
        targets = {
            t for t in asyncio.all_tasks()
            if t is not me and not t.done() and _stack_touches(t, markers)
        }
        if not targets:
            if attempt:
                log.warning(
                    "%s: lingering DB task(s) cleared after %d cancel round(s)",
                    phase, attempt,
                )
            return []
        for t in targets:
            t.cancel()
        await asyncio.wait(targets, timeout=grace)
    leftover = [t for t in targets if not t.done()]
    if leftover:
        log.critical(
            "%s: %d task(s) still pending after %d cancel round(s) — these can "
            "wedge event-loop close (TestClient portal join / systemd stop; "
            "issue #846):\n%s",
            phase, len(leftover), rounds, format_task_dump(leftover),
        )
    else:
        log.warning(
            "%s: lingering DB task(s) cleared after %d cancel round(s)",
            phase, rounds,
        )
    return leftover
