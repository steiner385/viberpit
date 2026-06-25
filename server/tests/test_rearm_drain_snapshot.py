import asyncio, json
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


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
        await asyncio.sleep(0.05)                            # let the clear task run (async DB needs >1 yield)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
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
        await asyncio.sleep(0.05)                            # let the clear task run (async DB needs >1 yield)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

    asyncio.run(run())
