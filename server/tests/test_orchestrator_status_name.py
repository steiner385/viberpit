"""Tests for orchestrator status persistence + Claude auto-naming."""
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
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_persist_status_updates_record(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "waiting_input")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_input"


def test_persist_status_never_overwrites_ended(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "ended")
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1").status
    assert asyncio.run(run()) == "ended"


def test_rename_pins_name_is_auto_false(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch.rename_session("s1", "My Name")
        rec = orch.record("s1")
        return rec.name, rec.name_is_auto
    name, is_auto = asyncio.run(run())
    assert name == "My Name"
    assert is_auto is False


def test_auto_name_applied_on_done(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"ai-title": "Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name
    assert asyncio.run(run()) == "Fix the login bug"


def test_auto_name_not_applied_when_manual(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"ai-title": "Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path, name="Hand Picked", name_is_auto=False)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name
    assert asyncio.run(run()) == "Hand Picked"


def test_persist_sub_agents_called_on_non_done_status(tmp_path):
    """FIX C: _persist_sub_agents must be called on every status persist, not only 'done'.

    Verifies that after a non-'done' status (e.g. 'working'), the in-memory record's
    sub_agents field is refreshed so the dashboard list (polled every 5s) shows
    sub-agent rows while the session is still active.

    We patch sub_agents() to return a fake summary and confirm it lands on the record
    after a 'working' status persist.
    """
    import asyncio as _asyncio

    async def run():
        orch = await _build_orch(tmp_path)
        fake_summary = [{"agentId": "toolu_live", "parentAgentId": None,
                         "agentType": "Explore", "description": "live task", "status": "running"}]

        # Patch sub_agents() to return our fake summary synchronously.
        async def _fake_sub_agents(session_id):
            return fake_summary

        orch.sub_agents = _fake_sub_agents

        # Trigger a non-'done' status persist.
        await orch._persist_status("s1", "working")
        return orch.record("s1").sub_agents

    result = asyncio.run(run())
    assert result == [{"agentId": "toolu_live", "parentAgentId": None,
                       "agentType": "Explore", "description": "live task", "status": "running"}], (
        f"sub_agents must be refreshed on 'working' status persist; got {result!r}"
    )
