"""_persist_status logs and audits the hand_back reason when a driver carries one —
spec docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md §6."""
from __future__ import annotations
import asyncio
import logging
import types

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                         engine_session_id="claude-s1", status="idle", cwd="/repo/s1"))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _audits(type_: str) -> list[AuditEvent]:
    async with db_session() as db:
        return list((await db.execute(
            sa_select(AuditEvent).where(AuditEvent.type == type_))).scalars().all())


def test_waiting_question_with_a_reason_persists_the_status_and_audits_the_reason(tmp_path, caplog):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="need a scope decision")
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            final = await orch._persist_status("s1", "waiting_question")
        rec = orch.record("s1")
        async with db_session() as db:
            row = (await db.execute(sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return final, rec, row, await _audits("session.hand_back")
    final, rec, row, audits = asyncio.run(run())

    assert final == "waiting_question"
    assert rec.status == "waiting_question" and row.status == "waiting_question"
    assert rec.last_turn_at is not None            # ACTIVITY_STATUSES: the clock advanced
    assert len(audits) == 1
    assert audits[0].detail == {"session_id": "s1", "reason": "need a scope decision"}
    assert audits[0].via == "session"
    assert any("session.hand_back" in r.getMessage() for r in caplog.records)


def test_waiting_question_without_a_reason_writes_no_audit(tmp_path):
    """A structured AskUserQuestion also lands at waiting_question; only a driver
    reporting a hand_back reason produces the audit row."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
        await orch._persist_status("s1", "waiting_question")
        return await _audits("session.hand_back")
    assert asyncio.run(run()) == []


def test_a_driver_without_the_attribute_is_tolerated(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace()
        return await orch._persist_status("s1", "waiting_question")
    assert asyncio.run(run()) == "waiting_question"


def test_a_done_status_never_audits_a_stale_reason(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        # engine_session_id=None: "done" runs _persist_status's completed_turn/parked
        # overlay (transcript_path -> _session_is_parked), an orthogonal codepath a
        # bare stub would otherwise crash inside (unrelated to the hand_back_reason
        # contract under test here); None routes it through the in-memory record's
        # own engine_session_id ("claude-s1") instead, and no real transcript file
        # existing simply reads as "not parked" (background_tasks.is_parked).
        orch._drivers["s1"] = types.SimpleNamespace(
            last_hand_back_reason="stale", engine_session_id=None)
        await orch._persist_status("s1", "done")
        return await _audits("session.hand_back")
    assert asyncio.run(run()) == []
