"""Regression tests for the aiosqlite worker-thread loop guard (issue #1134).

The flake these pin: an aiosqlite worker thread hands a result back with
``future.get_loop().call_soon_threadsafe(...)``; when the owning loop has closed
that raises, the ``except`` handler raises the *same* error again from the last
statement of the loop body, and the thread dies unhandled. pytest attributes the
resulting thread exception to whichever test happened to be running, so it
surfaced as an unrelated-looking failure in a different file each CI run — and
the dead worker leaves the SQLite handle open (``SAWarning: ... non-checked-in
connection``) and any later ``close()`` parked forever.

The loop closes under an in-flight operation because ``server.aio.bounded_wait``
abandons an overrunning teardown phase on purpose (#846/#881); under
``-n auto --dist loadfile`` the worker can be sitting in a 30 s ``busy_timeout``
lock wait when that happens, which is why it only ever reproduced under load.
"""
from __future__ import annotations

import asyncio
import gc
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
from queue import SimpleQueue

import aiosqlite
import aiosqlite.core as core
import pytest

from server.db.aiosqlite_guard import (
    build_loop_safe_worker,
    dropped_deliveries,
    install_loop_safe_worker,
    orphaned_worker_count,
    stop_orphaned_workers,
    upstream_worker,
)


def _closed_loop_future():
    """A future whose loop is already closed — the exact state a worker is left
    holding when its borrower is abandoned and the loop goes away."""
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    return fut


def _stop_item():
    """A queue item whose function returns aiosqlite's stop sentinel."""
    return (None, lambda: core._STOP_RUNNING_SENTINEL)


def _queue(*items) -> SimpleQueue:
    tx: SimpleQueue = SimpleQueue()
    for item in items:
        tx.put_nowait(item)
    return tx


def _run_worker(worker, tx, timeout=5.0) -> list[BaseException]:
    """Drive *worker* over *tx* on a thread; return whatever escaped it.

    A worker that never breaks blocks on ``tx.get()`` forever, so this must not
    join unboundedly — an unbounded join is the very hang being tested for.
    """
    escaped: list[BaseException] = []

    def run():
        try:
            worker(tx)
        except BaseException as exc:  # noqa: B036 — recording, not handling
            escaped.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "worker thread never returned — it ignored the stop sentinel"
    return escaped


# --------------------------------------------------------------------------- #
# The upstream defect                                                          #
# --------------------------------------------------------------------------- #
def test_upstream_worker_dies_when_its_loop_is_closed():
    """Pin aiosqlite's own behaviour, so the guard's reason to exist is visible.

    If this ever starts passing cleanly, upstream fixed it and
    ``server/db/aiosqlite_guard.py`` can be deleted.
    """
    tx = _queue((_closed_loop_future(), lambda: "result"), _stop_item())

    escaped = _run_worker(upstream_worker(), tx)

    assert [type(e) for e in escaped] == [RuntimeError]
    assert "Event loop is closed" in str(escaped[0])


# --------------------------------------------------------------------------- #
# The guard                                                                    #
# --------------------------------------------------------------------------- #
def test_guarded_worker_survives_a_closed_loop_and_keeps_serving():
    """A dead loop must cost one dropped delivery, not the worker thread."""
    served: list[str] = []
    before = dropped_deliveries()
    tx = _queue(
        (_closed_loop_future(), lambda: "orphaned"),
        (None, lambda: served.append("after")),
        _stop_item(),
    )

    escaped = _run_worker(build_loop_safe_worker(), tx)

    assert escaped == []
    assert served == ["after"], "worker stopped serving the queue after a dead loop"
    assert dropped_deliveries() == before + 1


def test_guarded_worker_stops_even_when_the_stop_delivery_fails():
    """The stop sentinel must be honoured independently of its own delivery.

    Upstream evaluates ``result is _STOP_RUNNING_SENTINEL`` *after* delivering,
    so a failed delivery skips the break. aiosqlite's worker threads are not
    daemon threads, so one that never breaks blocks interpreter exit — a
    process-level hang, strictly worse than the exception it replaced.
    """
    tx = _queue((_closed_loop_future(), lambda: core._STOP_RUNNING_SENTINEL))

    escaped = _run_worker(build_loop_safe_worker(), tx)

    assert escaped == []


def test_guarded_worker_still_delivers_results_and_exceptions():
    """The guard must not change the happy path: a live loop still gets both."""
    worker = build_loop_safe_worker()

    async def drive():
        loop = asyncio.get_running_loop()
        ok, boom = loop.create_future(), loop.create_future()
        tx = _queue(
            (ok, lambda: 41 + 1),
            (boom, _raise_value_error),
            _stop_item(),
        )
        t = threading.Thread(target=worker, args=(tx,), daemon=True)
        t.start()
        try:
            assert await asyncio.wait_for(ok, 5) == 42
            with pytest.raises(ValueError, match="boom"):
                await asyncio.wait_for(boom, 5)
        finally:
            t.join(5)
            assert not t.is_alive()

    asyncio.run(drive())


def _raise_value_error():
    raise ValueError("boom")


# --------------------------------------------------------------------------- #
# Orphaned workers must not block interpreter exit                             #
# --------------------------------------------------------------------------- #
def test_an_orphaned_worker_is_tracked_and_can_be_stopped():
    """Keeping a loop-orphaned worker alive is the point — it can still service
    the ``stop()`` GC enqueues. But aiosqlite's workers are not daemon threads,
    so one still waiting at interpreter exit would block the join; it has to be
    reachable and stoppable."""
    tx = _queue((_closed_loop_future(), lambda: "orphaned"))
    t = threading.Thread(target=build_loop_safe_worker(), args=(tx,), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while orphaned_worker_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert orphaned_worker_count() >= 1, "orphaned worker was never tracked"

        assert stop_orphaned_workers() >= 1
        t.join(5)
        assert not t.is_alive(), "orphaned worker ignored the shutdown stop"
    finally:
        stop_orphaned_workers()
        t.join(5)


def test_a_worker_that_never_lost_its_loop_is_not_tracked():
    async def drive():
        ok = asyncio.get_running_loop().create_future()
        tx = _queue((ok, lambda: "fine"), _stop_item())
        t = threading.Thread(target=build_loop_safe_worker(), args=(tx,), daemon=True)
        t.start()
        assert await asyncio.wait_for(ok, 5) == "fine"
        t.join(5)
        return orphaned_worker_count()

    before = orphaned_worker_count()
    assert asyncio.run(drive()) == before


def test_install_registers_the_pre_join_shutdown_hook():
    """``atexit`` runs AFTER ``threading._shutdown()`` joins non-daemon threads,
    i.e. after the hang. ``threading._register_atexit`` runs before it."""
    install_loop_safe_worker()
    # threading._register_atexit wraps the callback in a functools.partial.
    registered = [getattr(h, "func", h) for h in threading._threading_atexits]
    assert registered.count(stop_orphaned_workers) == 1, registered


def test_interpreter_exits_when_an_orphaned_connection_is_held_to_the_end():
    """The end-to-end guarantee, in a real subprocess: a connection whose loop
    died and which is never garbage collected must not hang process exit."""
    script = textwrap.dedent(
        """
        import asyncio, os, tempfile, time
        from server.db.aiosqlite_guard import install_loop_safe_worker
        import aiosqlite

        install_loop_safe_worker()
        fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        loop = asyncio.new_event_loop()

        async def start():
            conn = await aiosqlite.connect(path)
            inflight = asyncio.ensure_future(conn._execute(lambda: time.sleep(0.3)))
            await asyncio.sleep(0.05)
            return conn, inflight

        HELD, inflight = loop.run_until_complete(start())  # never collected
        inflight.cancel()
        loop.close()
        time.sleep(0.6)
        assert HELD._thread.is_alive(), "worker should have survived its loop"
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, timeout=60,
    )
    assert "OK" in proc.stdout, proc.stderr[-2000:]
    assert proc.returncode == 0, proc.stderr[-2000:]


# --------------------------------------------------------------------------- #
# Installation                                                                 #
# --------------------------------------------------------------------------- #
def test_install_is_idempotent_and_replaces_the_upstream_worker():
    assert install_loop_safe_worker() is True
    installed = core._connection_worker_thread
    assert installed is not upstream_worker()

    assert install_loop_safe_worker() is True
    assert core._connection_worker_thread is installed, "double-wrapped on re-install"


def test_make_engine_installs_the_guard(monkeypatch, tmp_path):
    """The engine factory is the seam: any SQLite engine implies aiosqlite
    connections, so the guard must be in place before the first one is opened."""
    from server.db import _make_engine

    monkeypatch.setattr(core, "_connection_worker_thread", upstream_worker())
    engine = _make_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    try:
        assert core._connection_worker_thread is not upstream_worker()
    finally:
        engine.sync_engine.dispose()


# --------------------------------------------------------------------------- #
# End to end — the CI signature itself                                         #
# --------------------------------------------------------------------------- #
def test_connection_survives_its_loop_closing_mid_operation(tmp_path):
    """Reproduce #1134's signature end to end: an operation still executing in
    the worker thread when its loop closes under an abandoned borrower."""
    install_loop_safe_worker()
    caught: list[str] = []
    prev_hook = threading.excepthook
    threading.excepthook = lambda args: caught.append(args.exc_type.__name__)
    try:
        loop = asyncio.new_event_loop()

        async def start():
            conn = await aiosqlite.connect(str(tmp_path / "e2e.sqlite"))
            await conn.execute("create table t (x int)")
            # A call still running in the worker when the loop dies — the shape
            # of a query parked in a 30 s busy_timeout lock wait under -n auto.
            inflight = asyncio.ensure_future(conn._execute(lambda: time.sleep(0.4)))
            await asyncio.sleep(0.05)
            return conn, inflight

        conn, inflight = loop.run_until_complete(start())
        inflight.cancel()  # borrower abandoned, bounded_wait style
        loop.close()
        time.sleep(0.8)

        assert caught == [], f"worker thread died unhandled: {caught}"
        assert conn._thread.is_alive(), "worker thread should have survived"

        # And it must still shut down cleanly rather than leak a non-daemon
        # thread: GC finalisation enqueues stop(), whose delivery also fails.
        thread = conn._thread
        del conn, inflight
        gc.collect()
        thread.join(5)
        assert not thread.is_alive(), "worker thread leaked past finalisation"
    finally:
        threading.excepthook = prev_hook
