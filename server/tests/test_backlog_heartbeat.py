"""Backlog runner ``last_active_at`` heartbeat.

The pr-doctor row gets a per-cycle ``last_active_at`` bump, but the backlog
runner never did — so its card's "last active" was permanently blank even while
the loop ticked and spawned sessions. One tick should bump the row while the
automation is enabled, and leave it stale when it is off.

Drives a single tick via ``_backlog_tick_once`` (extracted from the interval
loop) against a fake loop object, so the heartbeat is tested with no network.
"""
from __future__ import annotations

import asyncio


def _build_orch():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")

    ticks: list[int] = []

    class _FakeLoop:
        async def tick(self):
            ticks.append(1)

    orch._backlog_loop = _FakeLoop()
    return orch, ticks


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'hb.db'}")
    await registry.ensure_rows()


async def _set_row(*, enabled: bool, mode: str) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == "backlog-runner")
                         .values(enabled=enabled, mode=mode, last_active_at=None))


async def _last_active():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        row = (await db.execute(select(Automation).where(
            Automation.id == "backlog-runner"))).scalar_one()
        return row.last_active_at


def test_tick_bumps_last_active_when_enabled(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, ticks = _build_orch()
        await _set_row(enabled=True, mode="auto")

        await orch._backlog_tick_once()

        assert ticks == [1]  # the control loop still ran
        assert await _last_active() is not None  # heartbeat fired

    asyncio.run(run())


def test_tick_leaves_last_active_stale_when_off(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, ticks = _build_orch()
        # Disabled → effective mode "off" → no heartbeat (card should go stale).
        await _set_row(enabled=False, mode="auto")

        await orch._backlog_tick_once()

        assert ticks == [1]
        assert await _last_active() is None

    asyncio.run(run())
