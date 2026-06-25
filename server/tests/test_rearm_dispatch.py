# server/tests/test_rearm_dispatch.py
import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"description": "Watch PR", "command": "z"}, "description": "Watch PR"}]


def test_rearm_dispatches_one_turn_and_clears(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = {}
        class _D:  # stand-in driver
            async def send_prompt(self, text, images=None): sent["text"] = text
        async def _ensure(sid, identity="system"):
            orch._drivers[sid] = _D(); return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        n = await orch.rearm_background_tasks()
        assert n == 1
        assert "Watch PR" in sent["text"] and '"command": "z"' in sent["text"]
        assert "s1" in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

        # idempotent: a second pass does nothing (cleared + in _rearmed)
        assert await orch.rearm_background_tasks() == 0

    asyncio.run(run())


def test_rearm_resends_pending_prompt_only_and_clears(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rp.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_prompt="resume me"))   # only a prompt, no bg tasks
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = []
        class _D:
            async def send_prompt(self, text, images=None): sent.append(text)
        async def _ensure(sid, identity="system"):
            orch._drivers[sid] = _D(); return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        n = await orch.rearm_background_tasks()
        assert n == 1
        assert sent == ["resume me"]
        assert "s1" in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_prompt is None and row.pending_rearm is None

        # idempotent: a second pass does nothing
        assert await orch.rearm_background_tasks() == 0

    asyncio.run(run())


def test_rearm_failure_retains_snapshot(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r2.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        async def _boom(sid, identity="system"): raise RuntimeError("no account")
        orch.ensure_driver = _boom  # type: ignore

        assert await orch.rearm_background_tasks() == 0
        assert "s1" not in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm == _SNAP            # retained for retry

    asyncio.run(run())
