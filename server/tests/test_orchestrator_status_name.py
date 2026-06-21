"""Tests for orchestrator status persistence + AI auto-naming."""
import asyncio

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


class RecordingTitleFn:
    """Fake title generator: records calls, returns a fixed result."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, prompts, *, account, cwd):
        self.calls.append((prompts, account, cwd))
        return self.result


async def _build_orch(tmp_path, title_fn=None, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), title_fn=title_fn)
    await orch.rehydrate()
    return orch


async def _db_name(sid):
    from sqlalchemy import select as sa_select
    async with db_session() as db:
        row = (await db.execute(
            sa_select(DbSession).where(DbSession.id == sid))).scalars().first()
        return row.name


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


def _transcript_with_prompts(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type": "user", "message": {"content": "fix the login bug"}}\n'
    )
    return str(transcript)


def test_auto_name_generates_title_on_done(tmp_path):
    """A done turn on an auto-named session generates and persists a title."""
    title_fn = RecordingTitleFn("Fix the login bug")

    async def run():
        orch = await _build_orch(tmp_path, title_fn=title_fn)
        orch.transcript_path = lambda sid: _transcript_with_prompts(tmp_path)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name, await _db_name("s1")

    record_name, db_name = asyncio.run(run())
    assert record_name == "Fix the login bug"
    assert db_name == "Fix the login bug"           # persisted to DB too
    assert title_fn.calls[0][0] == ["fix the login bug"]  # got the user prompts


def test_auto_name_not_applied_when_manual(tmp_path):
    title_fn = RecordingTitleFn("Generated")

    async def run():
        orch = await _build_orch(
            tmp_path, title_fn=title_fn, name="Hand Picked", name_is_auto=False)
        orch.transcript_path = lambda sid: _transcript_with_prompts(tmp_path)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name

    assert asyncio.run(run()) == "Hand Picked"
    assert title_fn.calls == []                     # generator never invoked


def test_auto_name_skipped_when_account_limited(tmp_path):
    title_fn = RecordingTitleFn("Generated")

    async def run():
        orch = await _build_orch(tmp_path, title_fn=title_fn)
        orch.transcript_path = lambda sid: _transcript_with_prompts(tmp_path)
        orch._account_limited = lambda account: True
        await orch._persist_status("s1", "done")
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"
    assert title_fn.calls == []


def test_auto_name_skipped_when_no_prompts(tmp_path):
    title_fn = RecordingTitleFn("Generated")
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type": "assistant", "message": {"content": []}}\n')

    async def run():
        orch = await _build_orch(tmp_path, title_fn=title_fn)
        orch.transcript_path = lambda sid: str(empty)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"
    assert title_fn.calls == []


def test_auto_name_noop_when_generator_returns_none(tmp_path):
    title_fn = RecordingTitleFn(None)

    async def run():
        orch = await _build_orch(tmp_path, title_fn=title_fn)
        orch.transcript_path = lambda sid: _transcript_with_prompts(tmp_path)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"
    assert len(title_fn.calls) == 1                 # tried, degraded silently
