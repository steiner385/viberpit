"""A launch-time approval must not be refused by dispatch-outcome history.

The Backlog Runner writes its ``dispatch_outcomes`` row (``session_id`` set) at
dispatch time, BEFORE a model-approval hold is resolved. Approving the hold
deletes the ``pending_approval`` placeholder row so the spawn tail can re-INSERT
under the same id — and on Postgres that DELETE raised ForeignKeyViolationError
because the FK had no ON DELETE clause (the archive purge hit the same wall in
#2074). SQLite never caught it: FK enforcement is off there by default, so these
tests turn it on the way Postgres enforces it.

Two properties are pinned:
1. the FK is ``ON DELETE SET NULL`` (declared on the model, so create_all emits
   it on both dialects) — the delete itself can no longer fail;
2. the approve path RE-ATTACHES the history rows to the re-created session, on
   the happy path and on the restore-after-failed-spawn path, so an approved
   dispatch does not lose its session link.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, select

from server.db import db_session, init_db
from server.db.models import DispatchOutcome, Session as DbSession
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord

SID = "held-dispatch-1"


async def _seed(db_url: str) -> None:
    """A parked runner dispatch: pending_approval row + its running outcome row."""
    await init_db(db_url=db_url)
    from server import db as db_mod
    engine = db_mod._engine
    assert engine is not None

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    await engine.dispose()  # drop pooled conns so the pragma applies
    async with db_session() as db:
        db.add(DbSession(id=SID, name="n", account_name="acct", cwd="/tmp",
                         status="pending_approval", model=None, pending_model="fable",
                         origin="automation:backlog-runner",
                         pending_model_requested_by="policy"))
        await db.flush()
        db.add(DispatchOutcome(id="outcome-1", automation_id="backlog-runner",
                               target_ref="o/r#1210", session_id=SID, outcome="running"))


def _orchestrator(monkeypatch, *, spawn):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id=SID, name="n", account="acct", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="", cols=80, rows=24, cwd="/tmp", model=None,
                        status="pending_approval")
    rec.pending_model = "fable"
    o._records = {SID: rec}
    o._drivers = {}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o._pending_approvals = {SID: {"name": "n", "cwd": "/tmp", "account": "acct",
                                  "origin": "automation:backlog-runner"}}
    o._publish_event = lambda payload: None
    o._spawn_session = spawn
    o._active_session_count = lambda: 0

    async def _max_active():
        return 10
    o._max_active_sessions = _max_active

    async def _record(db, *, type, identity, detail=None, **kw):
        return None
    monkeypatch.setattr("server.sessions.orchestrator.record_audit", _record, raising=False)
    return o, rec


async def _outcome_session_id() -> str | None:
    async with db_session() as db:
        return (await db.execute(
            select(DispatchOutcome.session_id).where(DispatchOutcome.id == "outcome-1"))).scalar()


def test_approve_keeps_dispatch_outcome_attached_to_the_spawned_session(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}"

    async def run():
        await _seed(db_url)

        async def _spawn_session(session_id, *, model=None, **kw):
            # Mirror the real tail: it INSERTs its own row under the same id.
            async with db_session() as db:
                db.add(DbSession(id=session_id, name="n", account_name="acct", cwd="/tmp",
                                 status="in_progress", model=model))
            rec.status = "in_progress"
            rec.model = model
            return rec

        o, rec = _orchestrator(monkeypatch, spawn=_spawn_session)
        await o.resolve_model_approval(SID, "approve", identity="tony")
        assert rec.status == "in_progress"
        async with db_session() as db:
            row = await db.get(DbSession, SID)
            assert row is not None and row.status == "in_progress"
        assert await _outcome_session_id() == SID, (
            "the dispatch's history row must still point at the session it launched")

    asyncio.run(run())


def test_failed_approve_restores_the_hold_with_its_dispatch_outcome_attached(
        tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}"

    async def run():
        await _seed(db_url)

        async def _spawn_session(session_id, *, model=None, **kw):
            raise RuntimeError("worktree checkout failed")

        o, rec = _orchestrator(monkeypatch, spawn=_spawn_session)
        with pytest.raises(RuntimeError):
            await o.resolve_model_approval(SID, "approve", identity="tony")
        async with db_session() as db:
            row = await db.get(DbSession, SID)
            assert row is not None and row.status == "pending_approval"
            assert row.pending_model == "fable"
        assert SID in o._pending_approvals
        assert await _outcome_session_id() == SID, (
            "restoring the hold must re-attach the dispatch's history row too")

    asyncio.run(run())
