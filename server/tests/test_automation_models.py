"""Automation tables + Session provenance columns create and round-trip."""
from __future__ import annotations

import asyncio
from sqlalchemy import select

from server.db import init_db, db_session


def test_automation_row_round_trips(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        async with db_session() as db:
            db.add(Automation(id="pr-doctor", enabled=True, mode="auto", config={"repos": ["a/b"]}))
        async with db_session() as db:
            row = (await db.execute(select(Automation).where(Automation.id == "pr-doctor"))).scalar_one()
            assert row.enabled is True
            assert row.mode == "auto"
            assert row.config == {"repos": ["a/b"]}
            assert row.last_active_at is None

    asyncio.run(run())


def test_event_and_target_state_round_trip(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation, AutomationEvent, AutomationTargetState
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        async with db_session() as db:
            db.add(AutomationEvent(automation_id="pr-doctor", level="info", kind="detected",
                                   summary="CI fail", target="a/b#1", detail={"x": 1}))
            db.add(AutomationTargetState(automation_id="pr-doctor", target="a/b#1", state={"attempts": 1}))
        async with db_session() as db:
            ev = (await db.execute(select(AutomationEvent))).scalar_one()
            assert ev.kind == "detected" and ev.target == "a/b#1" and ev.session_id is None
            ts = (await db.execute(select(AutomationTargetState))).scalar_one()
            assert ts.state == {"attempts": 1}

    asyncio.run(run())


def test_session_has_origin_columns(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Session
        async with db_session() as db:
            db.add(Session(id="s1", name="n", account_name="acct", cwd="/tmp",
                           origin="pr-doctor", origin_ref="a/b#1"))
        async with db_session() as db:
            row = (await db.execute(select(Session).where(Session.id == "s1"))).scalar_one()
            assert row.origin == "pr-doctor" and row.origin_ref == "a/b#1"

    asyncio.run(run())
