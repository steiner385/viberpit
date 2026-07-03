"""Orchestrator teardown-completeness tests (issue #405).

The CI-only ``test_api`` batch flake was cross-test session-row contamination: a
fire-and-forget ``asyncio.create_task(...)`` spawned by one test survived that
test's app teardown and, because ``db_session()`` resolves the module-global
``_session_factory`` *at call time*, wrote into the NEXT test's freshly-installed
in-memory DB.

The fix makes ``SessionOrchestrator.stop()`` a complete teardown: every
fire-and-forget task is tracked (``_spawn``) and cancelled+awaited, the drain
self-clear timer is cancelled, and any still-live driver is closed. These tests
pin that contract deterministically (no reliance on the CI timing race).
"""
from __future__ import annotations

import asyncio

import pytest

from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    from server.sessions.orchestrator import SessionOrchestrator

    # poll_interval huge so the usage poller / monitors never tick during the test.
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


@pytest.mark.asyncio
async def test_spawn_tracks_and_stop_cancels(tmp_path):
    """A task spawned via ``_spawn`` is tracked and cancelled by ``stop()``."""
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    try:
        orch = _new_orch()

        async def _sleeper() -> None:
            await asyncio.sleep(3600)

        task = orch._spawn(_sleeper(), name="sleeper")
        assert task in orch._bg_tasks

        await orch.stop()

        assert task.done()
        assert task.cancelled()
        assert len(orch._bg_tasks) == 0
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_leaked_task_cannot_write_after_stop(tmp_path):
    """A leaked bg task, cancelled by ``stop()``, cannot contaminate a later DB.

    Reproduces the #405 contamination vector without the CI timing race: a bg task
    that would write a session row is held until *after* ``stop()`` and a fresh
    ``init_db()`` have run. Pre-fix ``stop()`` did not cancel it, so it wrote a
    foreign ``ghost-leak`` row into DB "b". Post-fix it is cancelled and DB "b"
    stays clean.
    """
    from sqlalchemy import select

    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    orch = _new_orch()

    release = asyncio.Event()
    started = asyncio.Event()

    async def _leaky_write() -> None:
        started.set()
        await release.wait()  # survive past stop() + the next init_db()
        async with db_session() as db:
            db.add(DbSession(
                id="ghost-leak", name="leak", account_name="acct",
                status="archived", cwd="/tmp/leak",
            ))

    orch._spawn(_leaky_write(), name="leaky")
    await started.wait()

    # Complete teardown must cancel the leaked task.
    await orch.stop()

    # Simulate the next test installing its own fresh DB (global factory swap).
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    try:
        release.set()
        await asyncio.sleep(0.05)  # give any surviving task a chance to fire

        async with db_session() as db:
            rows = (await db.execute(select(DbSession))).scalars().all()
        assert rows == [], f"leaked task contaminated DB b: {[r.id for r in rows]}"
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_stop_cancels_drain_selfclear_timer(tmp_path):
    """``begin_drain`` arms a self-clear timer task; ``stop()`` must cancel it."""
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    try:
        orch = _new_orch()
        await orch.begin_drain(selfclear_sec=3600)
        timer = orch._drain_timer
        assert timer is not None and not timer.done()

        await orch.stop()

        assert timer.done()
        assert orch._drain_timer is None
    finally:
        await dispose_db()
