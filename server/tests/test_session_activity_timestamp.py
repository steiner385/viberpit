"""The 'active … ago' label must track the most recent turn's *response*, not any
incidental row write.

Regression: `Session.updated_at` carries `onupdate=utcnow`, so every write (status
flip, sub-agent refresh, branch detect, auto-name, model/pin change) reset the clock —
collapsing the label to '0s ago' for every live session. The fix records a dedicated
`last_turn_at` that advances ONLY when the assistant hands output back: a finished turn
(done/complete/needs_attention) or a turn paused for the user (waiting_input/
waiting_question/ready_review).
"""
import asyncio

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.status import ACTIVITY_STATUSES
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _db_last_turn_at(tmp_path, sid="s1"):
    from sqlalchemy import select as sa_select
    async with db_session() as db:
        row = (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalars().first()
    return row.last_turn_at


# --- the status set is the single source of truth for "output handed back" ---

def test_activity_statuses_membership():
    # The assistant produced output and handed control back.
    for s in ("done", "complete", "needs_attention",
              "waiting_input", "waiting_question", "ready_review"):
        assert s in ACTIVITY_STATUSES, s
    # Working / never-ran / orchestrator overlays are NOT a response.
    for s in ("in_progress", "idle", "limited", "ended", "archived"):
        assert s not in ACTIVITY_STATUSES, s


# --- _persist_status only advances activity on a response ---

def test_in_progress_does_not_advance_activity(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1").last_turn_at, await _db_last_turn_at(tmp_path)
    rec_ts, db_ts = asyncio.run(run())
    assert rec_ts is None
    assert db_ts is None


def test_finished_turn_advances_activity_and_persists(tmp_path):
    # "complete" exercises the same ACTIVITY_STATUSES branch as "done" without the
    # auto-name side effect ("done" reads the account transcript/credentials).
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "complete")
        return orch.record("s1").last_turn_at, await _db_last_turn_at(tmp_path)
    rec_ts, db_ts = asyncio.run(run())
    assert rec_ts is not None
    assert db_ts is not None


def test_waiting_question_advances_activity(tmp_path):
    """User's chosen semantic: pausing to ask the user IS output handed back."""
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "waiting_question")
        return orch.record("s1").last_turn_at
    assert asyncio.run(run()) is not None


def test_resuming_work_does_not_rewind_or_reset_activity(tmp_path):
    """After a turn responds, starting the next turn (in_progress) must leave the
    'active … ago' clock pinned to that response — not reset it to now."""
    # Use "needs_attention" (a finished-turn ACTIVITY_STATUS) rather than "complete":
    # a `complete` status now universally schedules an auto-archive task, which fires on
    # the next `await` below and evicts "s1" from `_records` — orthogonal to the activity
    # clock this test pins. "needs_attention" advances last_turn_at with no archive side
    # effect (and, like "complete", no auto-name transcript read that "done" would trigger).
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "needs_attention")
        landed = orch.record("s1").last_turn_at
        await orch._persist_status("s1", "in_progress")
        return landed, orch.record("s1").last_turn_at
    landed, after = asyncio.run(run())
    assert after == landed


# (The dashboard wire mapping — last_turn_at → "updatedAt", fallback created_at — is
# pinned in test_record_to_dict_pin.py, the canonical home for _record_to_dict tests.)


# --- IPC contract round-trips the new field (remote/daemon mode) ---

def test_record_dict_roundtrip_preserves_last_turn_at():
    from datetime import datetime, timezone
    turn = datetime(2020, 1, 2, 3, 4, 5)
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="c",
        cols=80, rows=24, cwd="/x", last_turn_at=turn,
    )
    back = SessionRecord.from_dict(rec.to_dict())
    # The wire carries an explicit UTC offset now, so from_dict reconstructs a
    # tz-aware datetime — the SAME instant as the naive UTC input.
    assert back.last_turn_at == turn.replace(tzinfo=timezone.utc)


def test_to_dict_updatedat_is_last_turn_not_row_write():
    """The RPC `to_dict` must use the SAME meaning of `updatedAt` as the HTTP
    `_record_to_dict` — the last-turn time, not the raw row-write column — so a consumer
    reading either serializer's `updatedAt` never gets the "0s ago" row-write value."""
    from datetime import datetime
    turn = datetime(2020, 1, 2, 3, 4, 5)
    row_write = datetime(2020, 6, 6, 6, 6, 6)  # later, bumped by an incidental write
    created = datetime(2020, 1, 1, 0, 0, 0)
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="c", cols=80, rows=24, cwd="/x",
        created_at=created, updated_at=row_write, last_turn_at=turn,
    )
    assert rec.to_dict()["updatedAt"] == "2020-01-02T03:04:05+00:00"  # not row_write; UTC-aware


def test_to_dict_updatedat_falls_back_to_created_at():
    from datetime import datetime
    created = datetime(2020, 1, 1, 0, 0, 0)
    row_write = datetime(2020, 6, 6, 6, 6, 6)
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="c", cols=80, rows=24, cwd="/x",
        created_at=created, updated_at=row_write, last_turn_at=None,
    )
    assert rec.to_dict()["updatedAt"] == "2020-01-01T00:00:00+00:00"  # not row_write; UTC-aware
