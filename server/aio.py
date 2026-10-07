"""Shared asyncio teardown helpers.

``bounded_wait`` is the single implementation of "await this, but never hang":
give the awaitable a budget, then cancel it, then give the cancellation a grace
window, then abandon it with a loud log that NAMES THE CULPRIT (its live await
chain) and reports what it eventually did. Shutdown paths must use this instead of
bare ``await`` — an unbounded await in teardown wedged CI's TestClient exits for
11+ minutes until the job's wall-clock timeout killed pytest (issue #846).

``cancel_and_reap`` is the single implementation of "cancel this child and wait
for it, without swallowing MY OWN cancellation" (issue #1164).

``run_to_completion`` is the single implementation of the inverse: "finish this
even if I am cancelled, THEN propagate my cancellation" (issue #2726).

``caused_by_cancellation`` is the single implementation of "is this concrete
exception really a driver-wrapped ``CancelledError``?" (issues #1950, #2751).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Coroutine, Iterable, Iterator
from typing import Any, TypeVar

_T = TypeVar("_T")

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
    unwind; if it does not unwind in time it is abandoned rather than awaited
    forever — a wedged teardown phase must cost seconds, not the process.
    Exceptions from *aw* are logged, not raised.

    Abandonment is LOUD AND SPECIFIC: the CRITICAL carries *phase* **and the
    task's live await chain** (``format_task_dump``), and ``_watch_abandoned``
    reports what the task eventually did. Naming only the phase is what left
    ``stall.interrupt:<sid>`` undiagnosable across 15 prod occurrences in 11 days
    (issue #1539) — a bounded wait that cannot say what it bounded only converts
    a hang into a mystery.

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
    # Abandoning. Name the culprit while it is still parked — this is the half of
    # the #846 contract bounded_wait used to skip. Without the dump the CRITICAL
    # says only which phase blew its budget, which is why 15 occurrences of
    # 'stall.interrupt:<sid>' over 11 days identified nothing (issue #1539): every
    # candidate absorber on that path honours an external cancel when probed
    # alone, so the answer was only ever going to come from the live await chain.
    log.critical(
        "bounded_wait: phase %r did not unwind within %.1fs of cancellation, after "
        "a %.1fs budget — abandoning it (issue #846). Await chain:\n%s",
        phase, cancel_grace, timeout, format_task_dump([task]),
    )
    _watch_abandoned(task, phase)
    return False


def _watch_abandoned(
    task: asyncio.Future[Any], phase: str, *, level: int = logging.WARNING
) -> None:
    """Report an abandoned phase's eventual fate instead of dropping it.

    Two things go wrong when nobody holds the reference. A phase that later raises
    surfaces through asyncio's default handler as "Task exception was never
    retrieved" — real noise, with no phase name to route it back to the abandonment
    that caused it. And a phase that was merely SLOW (it unwound, or even completed,
    a second after we gave up) is indistinguishable in the logs from one genuinely
    stuck forever, so the abandonment log can never be confirmed or falsified.

    The callback costs nothing while the task runs and turns the abandonment into a
    measured event: which phase, how long after we gave up, and whether it unwound,
    failed, or completed. A recurring CRITICAL whose companion line always says
    "completed" is a budget that is too small, not a wedge.

    *level* mirrors ``_consume``'s: ``bounded_wait`` abandons loudly, while
    ``cancel_and_reap`` stays at DEBUG — it is a shutdown path whose previous
    behaviour was to swallow silently, and raising its volume now would be a
    behaviour change dressed as a diagnostic.
    """
    abandoned_at = time.monotonic()

    def _report(t: asyncio.Future[Any]) -> None:
        late = time.monotonic() - abandoned_at
        if t.cancelled():
            log.log(level, "abandoned phase %r unwound %.1fs later", phase, late)
            return
        exc = t.exception()
        if exc is not None:
            log.log(level, "abandoned phase %r failed %.1fs later",
                    phase, late, exc_info=exc)
        else:
            log.log(level, "abandoned phase %r completed %.1fs later — it was slow, "
                    "not stuck", phase, late)

    task.add_done_callback(_report)


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


async def run_to_completion(coro: Coroutine[Any, Any, _T], *, name: str) -> _T:
    """Run *coro* to completion even if the caller is cancelled, then re-raise.

    For work whose interruption is worse than its delay: a half-finished cleanup,
    or a SQLite write. Cancelling a coroutine while its aiosqlite statement is in
    flight makes SQLAlchemy invalidate the connection mid-transaction. The
    abandoned handle keeps the database's single write lock until GC finalises
    it, so every other writer waits out the 30 s busy timeout and fails with
    "database is locked" (issue #2726).

    The shield is re-entered after every cancellation, so a repeated cancel
    cannot strand the work either. The caller's cancellation is NOT swallowed
    (issue #1164): it is re-raised once the work has finished. The work's own
    result or exception is returned or raised when the caller was not cancelled.
    """
    task = asyncio.create_task(coro, name=name)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                break  # the work itself was cancelled (loop teardown), not us
            cancelled = True
    if cancelled:
        # Retrieve the outcome so a failure is not reported as never-retrieved;
        # the caller's cancellation outranks it.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


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
        # Same shape as bounded_wait's abandonment: nobody holds the reference any
        # more, so a later failure would surface as an unattributed "Task exception
        # was never retrieved". Kept at DEBUG — this path's contract is to be quiet.
        _watch_abandoned(task, phase, level=logging.DEBUG)
        return False
    _consume(task, phase, level=logging.DEBUG)
    return True


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield *exc* and everything reachable via ``__cause__``/``__context__``.

    Each exception is yielded once, so a cyclic chain terminates.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for nxt in (cur.__context__, cur.__cause__):
            if nxt is not None:
                stack.append(nxt)


def caused_by_cancellation(exc: BaseException) -> bool:
    """True if *exc* or anything in its ``__cause__``/``__context__`` chain is a
    ``CancelledError``.

    A bare ``CancelledError`` raised out of an ``await`` would already skip an
    ``except Exception`` clause (it is a ``BaseException`` since Python 3.8), but
    some drivers (asyncpg/uvloop mid-handshake) catch the cancellation and
    re-raise a concrete exception type ``from`` it instead of propagating it
    unchanged — that lands in ``except Exception`` looking like a real DB failure
    even though it is just the task being cancelled (#1950, #2751, #2752).
    """
    return any(isinstance(e, asyncio.CancelledError) for e in iter_exception_chain(exc))


def current_task_cancelling() -> bool:
    """True if the running task has a pending ``cancel()`` request.

    ``Task.cancelling()`` stays non-zero after ``task.cancel()`` even when a
    driver swallowed the resulting ``CancelledError`` and raised something else
    ``from`` it — the signal that a driver-wrapped cancellation is OURS to
    propagate (cancel rule, #1164) rather than a cancellation internal to the
    driver that we may degrade over.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def ended_by_wrapped_cancellation(task: asyncio.Task[Any]) -> bool:
    """True if a DONE *task* was ``cancel()``-ed and finished with a driver-wrapped
    ``CancelledError`` rather than as cancelled.

    For done-callbacks that log a task's failure: ``task.cancelled()`` is False in
    that case, yet the task was stopped on purpose (daemon stop/redeploy) — not an
    auto-error (#2751). ``cancelling()`` keeps its count after the task finishes,
    so a cancellation internal to the driver, with no ``cancel()`` on this task,
    still reports as the failure it is.
    """
    if task.cancelled():
        return False
    exc = task.exception()
    return exc is not None and task.cancelling() > 0 and caused_by_cancellation(exc)


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
      ``finally`` — forever, if the worker thread already died. Upstream's
      worker dies whenever it services a future whose loop closed;
      ``server/db/aiosqlite_guard.py`` (issue #1134) removes that specific
      cause for every connection this process opens, but the reaper still owns
      the shield layer above, a worker wedged inside a ``busy_timeout`` lock
      wait, and any aiosqlite connection opened outside our engine factory.

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
