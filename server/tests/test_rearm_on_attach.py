import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"command": "z"}, "description": "Watch PR"}]


def test_attach_triggers_pending_rearm(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             claude_session_id="c1", pending_rearm=_SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = {}
        class _D:
            claude_session_id = "c1"
            async def send_prompt(self, text, images=None): sent["text"] = text
            async def start(self): pass
        # short-circuit the heavy build path: return a fake driver directly
        orig_ensure = orch.ensure_driver
        async def _ensure(sid, identity="system"):
            if sid in orch._drivers:
                return orch._drivers[sid]
            orch._drivers[sid] = _D(); orch._records[sid].detached = False
            await orch._maybe_rearm_on_attach(sid)     # the hook under test
            return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        d = await orch.ensure_driver("s1")
        assert "Watch PR" in sent["text"]
        assert "s1" in orch._rearmed
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None

    asyncio.run(run())
