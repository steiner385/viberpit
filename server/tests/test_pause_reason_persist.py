"""_pause_session / _persist_status / _reassign_locked keep paused_reason + paused_until
in sync between the record, the row and the WS status frame."""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

import pytest
from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                engine_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _row(sid="s1"):
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalars().first()


def test_pause_session_persists_reason_and_reset(tmp_path):
    reset = _dt.datetime(2026, 9, 4, 16, 10, tzinfo=_dt.timezone.utc)

    async def run():
        orch = await _build_orch(tmp_path)
        frames = []
        orch._publish_event = frames.append
        await orch._pause_session(orch.record("s1"), reason="all_accounts_busy",
                                  earliest_reset=reset)
        return orch.record("s1"), await _row(), frames
    rec, row, frames = asyncio.run(run())

    assert rec.status == "needs_attention"
    assert rec.paused_reason == "all_accounts_busy"
    assert rec.paused_until == _dt.datetime(2026, 9, 4, 16, 10)   # naive UTC
    assert row.paused_reason == "all_accounts_busy"
    assert row.paused_until == _dt.datetime(2026, 9, 4, 16, 10)
    status_frames = [f for f in frames if f.get("type") == "status"]
    assert status_frames[-1]["pausedReason"] == "all_accounts_busy"
    assert status_frames[-1]["pausedUntil"] == "2026-09-04T16:10:00+00:00"
    assert status_frames[-1]["usageStalled"] is True


def test_pause_session_without_reset_leaves_until_null(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._publish_event = lambda e: None
        await orch._pause_session(orch.record("s1"), reason="crash_cap")
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.paused_reason == "crash_cap" and rec.paused_until is None
    assert row.paused_reason == "crash_cap" and row.paused_until is None


def test_persist_status_clears_reason_on_non_stalled_status(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="needs_attention",
                                 paused_reason="all_accounts_busy",
                                 paused_until=_dt.datetime(2026, 9, 4, 16, 10))
        frames = []
        orch._publish_event = frames.append
        assert orch.record("s1").paused_reason == "all_accounts_busy"   # rehydrated
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1"), await _row(), frames
    rec, row, frames = asyncio.run(run())
    assert rec.paused_reason is None and rec.paused_until is None
    assert row.paused_reason is None and row.paused_until is None
    assert frames[-1]["usageStalled"] is False
    assert frames[-1]["pausedReason"] is None


def test_persist_status_keeps_reason_while_still_stalled(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="needs_attention",
                                 paused_reason="rotation_cap")
        orch._publish_event = lambda e: None
        await orch._persist_status("s1", "needs_attention")
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.paused_reason == "rotation_cap"
    assert row.paused_reason == "rotation_cap"


# --- _reassign_locked clears the fields on success (reload_session-style harness) ----

class _NullCtx:
    def __init__(self, sink): self.sink = sink
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt, *a, **k):
        self.sink.append(stmt)
        return None


class _NewDriver:
    def __init__(self, *a, **k):
        self.cwd = "/tmp"; self.mode = "interactive"; self.identity = k.get("identity")
        self.engine_session_id = "c1"; self.sent = []
    async def start(self): pass
    async def send_prompt(self, p): self.sent.append(p)
    async def set_model(self, m): pass


class _OldDriver:
    cwd = "/tmp"; mode = "interactive"; identity = SYSTEM_IDENTITY; engine_session_id = "c1"
    async def interrupt(self): pass
    async def aclose(self): pass


@pytest.mark.asyncio
async def test_reassign_clears_pause_fields(monkeypatch):
    stmts = []
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: _NewDriver(resume=kw.get("resume"), identity=kw.get("identity")))
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx(stmts))
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", status="needs_attention",
                        paused_reason="all_accounts_busy",
                        paused_until=_dt.datetime(2026, 9, 4, 16, 10))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {"s1": rec}; orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {"s1": "go"}; orch._turn_restart_at = {}; orch._swapping = {}
    orch._clock = time.time; orch._registry = None; orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None

    async def _persist_status(sid, status):  # awaited by the parked-status persist
        rec.status = status

    orch._persist_status = _persist_status
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    await orch._reassign(rec, "b", reason="manual_rotate")

    assert rec.account == "b"
    assert rec.paused_reason is None and rec.paused_until is None
    # The account UPDATE also nulls the two columns.
    compiled = str(stmts[-1].compile(compile_kwargs={"literal_binds": True}))
    assert "paused_reason" in compiled and "paused_until" in compiled
