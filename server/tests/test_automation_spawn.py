"""spawn_session stamps provenance, auto-emits, and de-dups non-terminal sessions."""
from __future__ import annotations

import asyncio

from server.db import init_db, db_session


async def _fake_create_session(*, origin=None, origin_ref=None, **kw):
    """Mimics orchestrator.create_session: persists a Session row with provenance."""
    from server.db.models import Session
    async with db_session() as db:
        s = Session(id=f"sess-{origin_ref}", name="auto", account_name="a", cwd="/tmp",
                    status="working", origin=origin, origin_ref=origin_ref)
        db.add(s)
    return s


def test_spawn_stamps_origin_and_emits(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_a.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
        rec = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
        assert rec.origin == "pr-doctor" and rec.origin_ref == "a/b#1"
        spawned = [e for e in await ctx.list_events() if e["kind"] == "spawned"]
        assert spawned and spawned[0]["sessionId"] == "sess-a/b#1"

    asyncio.run(run())


def test_spawn_dedups_non_terminal(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_b.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))

        calls = []

        async def counting_create(**kw):
            calls.append(kw)
            return await _fake_create_session(**kw)

        ctx = AutomationContext("pr-doctor", create_session=counting_create)
        first = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
        second = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
        assert len(calls) == 1  # second call short-circuited
        assert second.id == first.id
        assert any(e["kind"] == "skipped_duplicate" for e in await ctx.list_events())

    asyncio.run(run())
