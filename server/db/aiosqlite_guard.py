"""Loop-safe result delivery for aiosqlite's connection worker thread (issue #1134).

aiosqlite runs every SQLite call on a per-connection worker thread and hands the
result back with ``future.get_loop().call_soon_threadsafe(...)``. Upstream does
that **unguarded, twice**::

    try:
        result = function()
        if future:
            future.get_loop().call_soon_threadsafe(set_result, future, result)   # (a)
        if result is _STOP_RUNNING_SENTINEL:
            break
    except BaseException as e:
        if future:
            future.get_loop().call_soon_threadsafe(set_exception, future, e)     # (b)

If the loop that owns ``future`` has closed, (a) raises ``RuntimeError: Event
loop is closed``; the handler then attempts the *same* call at (b), which raises
the same error from the last statement of the loop body — so it escapes
``_connection_worker_thread`` and **kills the worker thread**. Two things follow:
the SQLite handle is never closed (SQLAlchemy later reports ``SAWarning: The
garbage collector is trying to clean up non-checked-in connection``), and any
subsequent ``Connection.close()`` parks forever on a queue nobody services —
the wedge ``server.aio.reap_lingering_db_tasks`` exists to survive.

A loop can close under an in-flight operation whenever a borrower is abandoned,
which this codebase does **by design**: ``server.aio.bounded_wait`` abandons a
teardown phase that overruns its budget rather than hanging the process (#846 /
#881). Under ``pytest -n auto --dist loadfile`` the worker can additionally sit
blocked for up to ``PRAGMA busy_timeout`` (30 s) on a contended lock, so the
window is wide and load-dependent — which is exactly why #1134 presented as a
nondeterministic ``GeneratorExit``/``Event loop is closed`` flake landing on a
different, innocent test file each run.

The fix is to deliver results through a helper that treats a dead loop as "there
is nobody left to tell" instead of a fatal error, and to honour the stop sentinel
even when its own delivery failed — otherwise a worker whose stop notification
could not be delivered would keep waiting on its queue.

Keeping the worker alive is what lets an abandoned connection still release its
SQLite handle: GC finalisation calls ``Connection.__del__ → stop()``, which needs
a live worker to run. The cost is that aiosqlite's worker threads are **not**
daemon threads, so one still waiting when the interpreter exits would block
``threading._shutdown()``'s join — so orphaned workers are tracked and stopped
from a ``threading._register_atexit`` hook, which runs *before* that join.

Installed from ``server.db._make_engine`` for SQLite URLs (the sole engine
factory), so every aiosqlite connection this process opens gets it.
"""
from __future__ import annotations

import logging
import threading
from queue import SimpleQueue
from types import ModuleType
from typing import Any, Callable

import aiosqlite.core as _aiosqlite_core

log = logging.getLogger(__name__)

# Captured at import, before anything can patch it — the reference implementation
# this module wraps, and what tests use to pin the upstream defect.
_UPSTREAM_WORKER: Callable[..., Any] = _aiosqlite_core._connection_worker_thread

_install_lock = threading.Lock()
_dropped_lock = threading.Lock()
_dropped_deliveries = 0

# Queues whose worker has outlived the loop it was reporting to. Keeping such a
# worker ALIVE is the point of this module — it can still service the ``stop()``
# that GC finalisation enqueues, which is how an abandoned connection releases
# its SQLite handle. But aiosqlite's worker threads are NOT daemon threads, so
# one still waiting on its queue when the interpreter exits blocks
# ``threading._shutdown()``'s join forever (verified: a connection kept alive by
# a live reference hangs process exit — which is already true of ANY aiosqlite
# connection opened and never closed, guard or no guard). So orphans are tracked
# and stopped from a ``threading._register_atexit`` hook, which runs BEFORE
# non-daemon threads are joined (``atexit`` runs after, and is too late).
_orphans_lock = threading.Lock()
_orphan_queues: set[Any] = set()
_atexit_registered = False


def upstream_worker() -> Callable[..., Any]:
    """aiosqlite's own ``_connection_worker_thread``, as it was at import."""
    return _UPSTREAM_WORKER


def dropped_deliveries() -> int:
    """How many results this process could not hand back because the owning loop
    had already closed. Non-zero is not an error — it counts borrowers abandoned
    by ``bounded_wait`` — but a rising count localises teardown overruns."""
    with _dropped_lock:
        return _dropped_deliveries


def _note_dropped() -> None:
    global _dropped_deliveries
    with _dropped_lock:
        _dropped_deliveries += 1


def orphaned_worker_count() -> int:
    """Workers currently outliving the loop they were reporting to."""
    with _orphans_lock:
        return len(_orphan_queues)


def stop_orphaned_workers() -> int:
    """Ask every orphaned worker to stop; return how many were asked.

    Registered to run from ``threading._shutdown()`` before it joins non-daemon
    threads. Enqueuing the stop sentinel breaks the worker's loop; the SQLite
    handle it may still hold is released by ``sqlite3.Connection``'s own
    finaliser, which is the right trade at interpreter exit.
    """
    with _orphans_lock:
        queues = list(_orphan_queues)
        _orphan_queues.clear()
    sentinel = _aiosqlite_core._STOP_RUNNING_SENTINEL
    for tx in queues:
        try:
            tx.put_nowait((None, lambda: sentinel))
        except Exception:  # pragma: no cover — a queue we can no longer reach
            pass
    return len(queues)


def build_loop_safe_worker(core: ModuleType = _aiosqlite_core) -> Callable[..., Any]:
    """Build a replacement for ``core._connection_worker_thread``.

    Mirrors upstream's loop exactly except that (1) result delivery cannot raise
    out of the thread and (2) the stop sentinel is honoured independently of
    whether its delivery succeeded. Takes *core* as a parameter so tests can
    exercise the function directly, without patching a module global.
    """
    stop_sentinel = core._STOP_RUNNING_SENTINEL
    set_result = core.set_result
    set_exception = core.set_exception

    def _deliver(future: Any, setter: Callable[..., Any], value: Any) -> bool:
        """Hand *value* back to *future*'s loop. True if the loop was gone."""
        if future is None:
            return False
        try:
            future.get_loop().call_soon_threadsafe(setter, future, value)
        except RuntimeError:
            # "Event loop is closed" — the borrower and its loop are gone, so
            # there is no one to deliver to. Upstream lets this kill the thread.
            _note_dropped()
            return True
        return False

    def _loop_safe_connection_worker_thread(tx: SimpleQueue[Any]) -> None:
        try:
            while True:
                future, function = tx.get()
                stop = False
                orphaned = False
                try:
                    result = function()
                    stop = result is stop_sentinel
                    orphaned = _deliver(future, set_result, result)
                except BaseException as exc:  # noqa: B036 — mirrors upstream
                    orphaned = _deliver(future, set_exception, exc)
                if orphaned:
                    with _orphans_lock:
                        _orphan_queues.add(tx)
                if stop:
                    break
        finally:
            with _orphans_lock:
                _orphan_queues.discard(tx)

    return _loop_safe_connection_worker_thread


def install_loop_safe_worker() -> bool:
    """Install the guard process-wide. Idempotent; returns True if it is active.

    ``aiosqlite.Connection.__init__`` resolves ``_connection_worker_thread``
    from the module globals when it builds the thread, so patching the module
    attribute covers every connection opened after this call.

    Returns False (and warns) if aiosqlite's internals no longer have the shape
    this guard wraps — a shim must degrade to upstream behaviour, not break the
    process.
    """
    with _install_lock:
        current = getattr(_aiosqlite_core, "_connection_worker_thread", None)
        if getattr(current, "_ccswitch_loop_safe", False):
            return True
        try:
            worker = build_loop_safe_worker(_aiosqlite_core)
        except AttributeError:
            log.warning(
                "aiosqlite.core no longer exposes the internals "
                "server/db/aiosqlite_guard.py wraps — leaving the upstream "
                "worker in place (issue #1134)"
            )
            return False
        worker._ccswitch_loop_safe = True  # type: ignore[attr-defined]
        # Private, but stable since 3.9 and exactly this use case — it is how
        # concurrent.futures stops its own worker threads. Plain ``atexit`` runs
        # AFTER threading._shutdown()'s join, i.e. after the hang.
        global _atexit_registered
        register = getattr(threading, "_register_atexit", None)
        if register is not None and not _atexit_registered:
            register(stop_orphaned_workers)
            _atexit_registered = True
        elif register is None:  # pragma: no cover — no CPython release drops this
            log.warning(
                "threading._register_atexit is unavailable — an orphaned "
                "aiosqlite worker could block interpreter exit (issue #1134)"
            )
        _aiosqlite_core._connection_worker_thread = worker
        return True
