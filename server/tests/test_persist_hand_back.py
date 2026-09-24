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


def test_handed_back_with_a_reason_persists_the_status_and_audits_the_reason(tmp_path, caplog):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="need a scope decision")
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            final = await orch._persist_status("s1", "handed_back")
        rec = orch.record("s1")
        async with db_session() as db:
            row = (await db.execute(sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return final, rec, row, await _audits("session.hand_back")
    final, rec, row, audits = asyncio.run(run())

    assert final == "handed_back"
    assert rec.status == "handed_back" and row.status == "handed_back"
    # #2321: the ask is also persisted, so the card can show WHAT is wanted without
    # the user opening the session. Before this it lived only in the audit row below.
    assert rec.hand_back_reason == "need a scope decision"
    assert row.hand_back_reason == "need a scope decision"
    assert rec.last_turn_at is not None            # ACTIVITY_STATUSES: the clock advanced
    assert len(audits) == 1
    assert audits[0].detail == {"session_id": "s1", "reason": "need a scope decision"}
    assert audits[0].via == "session"
    assert any("session.hand_back" in r.getMessage() for r in caplog.records)


def test_handed_back_without_a_reason_writes_no_audit(tmp_path):
    """A session may hand back silently; only a driver reporting a reason audits."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
        await orch._persist_status("s1", "handed_back")
        return await _audits("session.hand_back")
    assert asyncio.run(run()) == []


def test_a_real_question_card_never_claims_the_hand_back_reason(tmp_path):
    """A structured AskUserQuestion lands at waiting_question, NOT handed_back (#2321).
    The driver may still be carrying a stale reason from an earlier hand-back; neither
    the audit nor the persisted column may pick it up, or the card would show an ask
    belonging to a different turn. Before the split both states shared one status and
    this was told apart only by whether a reason happened to be present."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="stale ask")
        await orch._persist_status("s1", "waiting_question")
        rec = orch.record("s1")
        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return rec, row, await _audits("session.hand_back")
    rec, row, audits = asyncio.run(run())
    assert audits == []
    assert rec.hand_back_reason is None and row.hand_back_reason is None


def test_moving_off_handed_back_clears_the_persisted_reason(tmp_path):
    """The ask describes ONE turn. A session that hands back and is then resumed must
    not keep advertising what it wanted two turns ago — the row is cleared, not just
    left behind for the card's status gate to hide."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="need a decision")
        await orch._persist_status("s1", "handed_back")
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
        await orch._persist_status("s1", "in_progress")
        rec = orch.record("s1")
        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return rec, row
    rec, row = asyncio.run(run())
    assert rec.hand_back_reason is None and row.hand_back_reason is None


def test_a_driver_without_the_attribute_is_tolerated(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace()
        return await orch._persist_status("s1", "handed_back")
    assert asyncio.run(run()) == "handed_back"


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
