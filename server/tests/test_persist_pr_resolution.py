"""_persist_status writes pr_resolution / pr_resolution_source when a driver carries a marker."""
from __future__ import annotations
import asyncio
import types

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _db_row(tmp_path, sid="s1"):
    async with db_session() as db:
        row = (await db.execute(
            sa_select(DbSession).where(DbSession.id == sid)
        )).scalars().first()
    return row


def test_complete_with_marker_persists_resolution(tmp_path):
    """When driver carries last_pr_resolution and status=='complete', both the in-memory
    record and DB row should be updated with pr_resolution and pr_resolution_source."""
    async def run():
        orch = await _build_orch(tmp_path)
        # Inject a stub driver that reports a resolution
        orch._drivers["s1"] = types.SimpleNamespace(last_pr_resolution="not-needed")
        await orch._persist_status("s1", "complete")
        rec = orch.record("s1")
        row = await _db_row(tmp_path)
        return rec, row
    rec, row = asyncio.run(run())

    assert rec.pr_resolution == "not-needed"
    assert rec.pr_resolution_source == "marker"
    assert row.pr_resolution == "not-needed"
    assert row.pr_resolution_source == "marker"


def test_non_complete_status_does_not_persist_resolution(tmp_path):
    """A non-complete status (e.g. in_progress) should NOT write pr_resolution."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_pr_resolution="not-needed")
        await orch._persist_status("s1", "in_progress")
        rec = orch.record("s1")
        row = await _db_row(tmp_path)
        return rec, row
    rec, row = asyncio.run(run())

    assert rec.pr_resolution is None
    assert row.pr_resolution is None


def test_pre_existing_resolution_not_clobbered(tmp_path):
    """If the DB row already has a pr_resolution (e.g. from classifier), don't overwrite."""
    async def run():
        orch = await _build_orch(tmp_path, pr_resolution="needed",
                                 pr_resolution_source="classifier")
        orch._drivers["s1"] = types.SimpleNamespace(last_pr_resolution="not-needed")
        await orch._persist_status("s1", "complete")
        rec = orch.record("s1")
        row = await _db_row(tmp_path)
        return rec, row
    rec, row = asyncio.run(run())

    # Original classifier value must be preserved
    assert rec.pr_resolution == "needed"
    assert rec.pr_resolution_source == "classifier"
    assert row.pr_resolution == "needed"
    assert row.pr_resolution_source == "classifier"


def test_complete_without_driver_is_a_no_op(tmp_path):
    """If there's no driver for the session, pr_resolution stays None."""
    async def run():
        orch = await _build_orch(tmp_path)
        # No driver injected
        await orch._persist_status("s1", "complete")
        rec = orch.record("s1")
        row = await _db_row(tmp_path)
        return rec, row
    rec, row = asyncio.run(run())

    assert rec.pr_resolution is None
    assert row.pr_resolution is None
