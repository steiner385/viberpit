"""Regression tests for reap_lingering_db_tasks (issue #846).

Reproduces the REAL wedge that hung CI teardowns: an aiosqlite connection
whose worker thread died (it dies when it services a future belonging to a
closed loop — a cross-test leak under -n auto load) leaves any subsequent
``Connection.close()`` awaiting a future nobody will ever resolve. Delivering
a single cancel does not help — ``close()`` catches it at the ``_execute``
await and then awaits ``stop()``'s (equally dead) future in its ``finally`` —
which is exactly why asyncio's loop-close ``_cancel_all_tasks`` (ONE cancel,
then an unbounded gather) hangs the anyio portal join in TestClient.__exit__.

The reaper cancels repeatedly, peeling one absorb layer per round.
"""
from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from server.aio import reap_lingering_db_tasks


def _kill_worker(conn: aiosqlite.Connection) -> None:
    """Make the connection's worker thread die the way it dies in the wild.

    The worker services queue items by resolving their futures via
    ``future.get_loop().call_soon_threadsafe(...)``; when the future's loop is
    already CLOSED that raises, its ``except`` handler raises again trying to
    set the exception, and the thread exits without draining the queue.
    (aiosqlite 0.22.x ``core._connection_worker_thread``.)
    """
    dead_loop = asyncio.new_event_loop()
    fut = dead_loop.create_future()
    dead_loop.close()
    conn._tx.put_nowait((fut, lambda: None))


async def _wedged_close_task(tmp_path) -> tuple[asyncio.Task, aiosqlite.Connection]:
    conn = await aiosqlite.connect(str(tmp_path / "reap-test.sqlite"))
    await conn.execute("select 1")
    _kill_worker(conn)
    deadline = time.monotonic() + 5.0
    while conn._thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not conn._thread.is_alive(), "worker thread should have died"
    task = asyncio.get_running_loop().create_task(
        conn.close(), name="wedged-aiosqlite-close"
    )
    await asyncio.sleep(0.1)  # parked at `await self._execute(...)`, dead worker
    return task, conn


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"  # the killed worker
)
def test_single_cancel_does_not_clear_the_wedge_but_reap_does(tmp_path):
    """Pin the failure mode AND the fix in one scenario.

    First half documents WHY loop close hangs: one cancel (all that
    ``_cancel_all_tasks`` ever sends) is absorbed by aiosqlite close()'s
    ``finally`` and the task stays pending. Second half: the reaper's repeated
    rounds clear it.
    """
    async def run():
        task, _conn = await _wedged_close_task(tmp_path)

        # One cancel — the most the loop's own close would deliver.
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=0.5)
        assert task in pending, (
            "expected the close task to absorb a single cancel (finally-await "
            "on the dead worker's stop future) — if this starts completing, "
            "aiosqlite changed and the reaper may be droppable"
        )

        leftover = await reap_lingering_db_tasks(grace=0.3)
        assert leftover == []
        assert task.done()

    asyncio.run(run())


def test_reap_is_a_noop_when_nothing_is_wedged():
    async def run():
        leftover = await reap_lingering_db_tasks(grace=0.1)
        assert leftover == []

    asyncio.run(run())


def test_reap_leaves_non_db_tasks_alone():
    async def run():
        gate = asyncio.Event()
        bystander = asyncio.get_running_loop().create_task(
            gate.wait(), name="innocent-bystander"
        )
        await asyncio.sleep(0.05)
        leftover = await reap_lingering_db_tasks(grace=0.1)
        assert leftover == []
        assert not bystander.cancelled() and not bystander.done()
        gate.set()
        await bystander

    asyncio.run(run())


def test_reap_peels_multiple_cancel_absorbing_layers():
    """A task that swallows the first cancel and re-parks (the SQLAlchemy
    shield shape) must be cleared by a later round, not abandoned after one."""
    async def run():
        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # absorb round 1's cancel and park again; round 2's gets through
                await asyncio.Event().wait()

        task = asyncio.get_running_loop().create_task(
            stubborn(), name="stubborn-db-task"
        )
        await asyncio.sleep(0.05)
        # Match on THIS file so the fake task counts as a "DB" task.
        leftover = await reap_lingering_db_tasks(
            grace=0.2, markers=(__file__,)
        )
        assert leftover == []
        assert task.done()

    asyncio.run(run())


def test_reap_reports_a_truly_unkillable_task():
    """A task that ignores every cancel must be returned (and CRITICAL-logged
    with its stack) rather than looping forever — the reaper itself is bounded."""
    async def run():
        release = asyncio.Event()

        async def immortal():
            while True:
                try:
                    await release.wait()
                    return
                except asyncio.CancelledError:
                    continue  # ignore every cancel

        task = asyncio.get_running_loop().create_task(
            immortal(), name="immortal-db-task"
        )
        await asyncio.sleep(0.05)
        start = time.monotonic()
        leftover = await reap_lingering_db_tasks(
            rounds=3, grace=0.1, markers=(__file__,)
        )
        elapsed = time.monotonic() - start
        assert [t.get_name() for t in leftover] == ["immortal-db-task"]
        assert elapsed < 2.0, f"reap must stay bounded, took {elapsed:.1f}s"
        release.set()  # let it finish so the loop closes cleanly
        await task

    asyncio.run(run())


def test_lifespan_shutdown_runs_the_reap(tmp_path, monkeypatch):
    """The app lifespan must invoke the reaper as its final shutdown phase."""
    from fastapi.testclient import TestClient

    import server.aio as aio_mod
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    calls: list[str] = []
    real = aio_mod.reap_lingering_db_tasks

    async def spy(**kw):
        calls.append("reap")
        return await real(**kw)

    # app.py's shutdown branch does `from server.aio import reap_lingering_db_tasks`
    # at RUNTIME, so patching the module attribute is what it will resolve.
    monkeypatch.setattr(aio_mod, "reap_lingering_db_tasks", spy)
    monkeypatch.setenv(
        "CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    )

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False):
        pass
    assert calls == ["reap"]
