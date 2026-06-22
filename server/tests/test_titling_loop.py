"""Tests for the periodic titling sweep — adopts late-written titles with no new turn."""
import asyncio

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="in_progress", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_titling_sweep_adopts_title_without_a_turn(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Investigate slow query"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()           # no _persist_status / no "done"
        return orch.record("s1").name

    assert asyncio.run(run()) == "Investigate slow query"


def test_titling_sweep_skips_ended_sessions(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Should not apply"}\n')

    async def run():
        # Seed as idle so rehydrate loads it, then simulate mid-run transition to ended.
        orch = await _build_orch(tmp_path, status="idle")
        orch.record("s1").status = "ended"
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"


def test_titling_interval_defaults_to_15s(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        return orch._titling_interval
    assert asyncio.run(run()) == 15.0
