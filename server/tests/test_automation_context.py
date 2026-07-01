"""AutomationContext emit (cap + filters) and per-target state."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.db import init_db, db_session


def test_emit_appends_and_lists_newest_first(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", event_cap=5)
        await ctx.emit("info", "first", kind="detected", target="a/b#1")
        await ctx.emit("warn", "second", kind="attempted", target="a/b#1")
        events = await ctx.list_events()
        assert [e["summary"] for e in events] == ["second", "first"]
        assert events[0]["kind"] == "attempted"

    asyncio.run(run())


def test_emit_prunes_to_cap(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", event_cap=5)
        for i in range(8):  # cap is 5
            await ctx.emit("info", f"e{i}")
        events = await ctx.list_events(limit=100)
        assert len(events) == 5
        assert events[0]["summary"] == "e7"  # newest kept

    asyncio.run(run())


def test_list_events_filters(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", event_cap=5)
        await ctx.emit("info", "x", kind="detected", target="a/b#1")
        await ctx.emit("info", "y", kind="attempted", target="a/b#2")
        assert [e["summary"] for e in await ctx.list_events(target="a/b#1")] == ["x"]
        assert [e["summary"] for e in await ctx.list_events(kind="attempted")] == ["y"]

    asyncio.run(run())


def test_emit_swallows_errors(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", event_cap=5)
        await ctx.emit("info", "ok")  # baseline works
        with patch("server.automations.context.db_session", side_effect=RuntimeError("boom")):
            await ctx.emit("info", "should not raise")  # no exception

    asyncio.run(run())


def test_target_state_round_trip(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", event_cap=5)
        assert await ctx.get_target_state("a/b#1") == {}
        await ctx.set_target_state("a/b#1", {"attempts": 2})
        assert await ctx.get_target_state("a/b#1") == {"attempts": 2}
        await ctx.set_target_state("a/b#1", {"attempts": 3})
        assert await ctx.get_target_state("a/b#1") == {"attempts": 3}

    asyncio.run(run())
