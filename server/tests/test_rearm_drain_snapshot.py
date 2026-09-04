import asyncio, json
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _poll_until_cleared(session_id, attr, *, timeout=5.0):
    """Wait for a DbSession column to be cleared to None by the async clear task.

    Replaces a fixed ``asyncio.sleep(0.05)`` that raced the clear task: under the
    ``-n auto`` server suite the CI runner is heavily loaded, so a single short
    yield was not always enough for the async DB write to commit, and the assert
    intermittently saw the pre-clear value (flaky 300s-adjacent failures). Polls
    on a short interval up to ``timeout`` and returns the row either way, so a
    genuine regression still fails the following assert with the stale value.
    """
    import time
    deadline = time.monotonic() + timeout
    while True:
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == session_id))).scalar_one()
        if getattr(row, attr) is None or time.monotonic() >= deadline:
            return row
        await asyncio.sleep(0.02)


def _seed_transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "uuid": "u",
        "message": {"content": [{"type": "tool_use", "id": "toolu_mon", "name": "Monitor",
                                 "input": {"description": "Watch PR", "command": "z"}}]}}) + "\n")
    return str(p)


class _PromptDriver:
    """Live-driver stand-in whose only relevant method is the snapshot accessor."""
    def pending_foreground_prompt(self):
        return "the prompt"


def test_drain_snapshots_foreground_prompt(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path)))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        orch._drivers["s1"] = _PromptDriver()                 # live turn, no output yet
        orch.transcript_path = lambda sid: None               # type: ignore  # no bg tasks

        await orch.begin_drain(selfclear_sec=999)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_prompt == "the prompt"

        orch.cancel_drain()
        row = await _poll_until_cleared("s1", "pending_prompt")  # deterministic wait for the clear task
        assert row.pending_prompt is None

    asyncio.run(run())


def test_begin_drain_snapshots_open_tasks_then_cancel_clears(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path)))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        orch._drivers["s1"] = object()                       # pretend a live process
        orch.transcript_path = lambda sid: _seed_transcript(tmp_path)  # type: ignore

        await orch.begin_drain(selfclear_sec=600)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm and row.pending_rearm[0]["tool_use_id"] == "toolu_mon"

        orch.cancel_drain()
        row = await _poll_until_cleared("s1", "pending_rearm")  # deterministic wait for the clear task
        assert row.pending_rearm is None

    asyncio.run(run())
