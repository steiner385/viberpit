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


def test_attach_during_the_boot_pass_never_replays_the_prompt_twice(tmp_path):
    """The boot re-arm runs behind the daemon's yield now, so an attach can call
    `_maybe_rearm_on_attach` while the boot pass is still inside `ensure_driver` for
    the same session. Both would read the same snapshot and both send it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                             pending_prompt="resume me"))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        sent = []
        gate = asyncio.Event()
        class _D:
            async def send_prompt(self, text, images=None): sent.append(text)
        async def _ensure(sid, identity="system"):
            await gate.wait()  # the slow resume the attach races against
            orch._drivers.setdefault(sid, _D()); return orch._drivers[sid]
        orch.ensure_driver = _ensure  # type: ignore

        boot = asyncio.create_task(orch.rearm_background_tasks())
        await asyncio.sleep(0.05)
        attach = asyncio.create_task(orch._maybe_rearm_on_attach("s1"))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(boot, attach)
        assert sent == ["resume me"]

    asyncio.run(run())
