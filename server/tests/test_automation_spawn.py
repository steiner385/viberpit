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


def test_spawn_ignores_inactive_duplicates(tmp_path):
    """An archived (or ended) prior session for the same target must NOT block a
    re-spawn — regression for the Resolve-conflicts button silently returning an
    archived session while toasting 'Spawned fix session'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_c.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation, INACTIVE_SESSION_STATUSES, Session
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
            for i, status in enumerate(sorted(INACTIVE_SESSION_STATUSES)):
                db.add(Session(id=f"old-{i}", name="auto", account_name="a", cwd="/tmp",
                               status=status, origin="pr-doctor", origin_ref="a/b#1"))

        ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
        rec = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
        assert rec.id == "sess-a/b#1"  # fresh spawn, not an inactive leftover
        assert not any(e["kind"] == "skipped_duplicate" for e in await ctx.list_events())

    asyncio.run(run())


def test_spawn_defers_when_fleet_has_no_capacity(tmp_path):
    """#2209: a fleet-wide "no capacity" refusal is routine and self-healing. It must
    be recorded as a soft "info" deferral and return None — not escape spawn_session
    as a raw exception that the caller's catch-all logs at WARNING and the error
    sink files as a new auto-error issue on every occurrence."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_d.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        from server.sessions.fleet_router import NoHostAvailable
        async with db_session() as db:
            db.add(Automation(id="pm-classify"))

        async def saturated_create(**kw):
            raise NoHostAvailable(
                "no enabled, healthy host with capacity; refusing to run in-process "
                "to avoid a split-brain over the shared database")

        ctx = AutomationContext("pm-classify", create_session=saturated_create)
        rec = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
        assert rec is None
        events = await ctx.list_events()
        deferred = [e for e in events if e["kind"] == "spawn_deferred"]
        assert len(deferred) == 1
        assert deferred[0]["level"] == "info"
        assert "a/b#1" in deferred[0]["summary"]
        assert not any(e["kind"] == "spawned" for e in events)

    asyncio.run(run())


def test_spawn_defers_when_no_host_can_run_the_engine(tmp_path):
    """The engine-specific subclass is a NoHostAvailable too — same soft deferral."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_e.db'}"

    async def run():
        await init_db(db_url=db_url)
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        from server.sessions.fleet_router import NoEngineHostAvailable
        from ccswitch.engines import AgentEngine
        async with db_session() as db:
            db.add(Automation(id="pm-classify"))

        async def no_codex_create(**kw):
            raise NoEngineHostAvailable(AgentEngine.CODEX, {"rocky": "codex not installed"})

        ctx = AutomationContext("pm-classify", create_session=no_codex_create)
        assert await ctx.spawn_session(origin_ref="a/b#2", name="auto") is None
        assert any(e["kind"] == "spawn_deferred" for e in await ctx.list_events())

    asyncio.run(run())


def test_spawn_still_raises_unrelated_create_errors(tmp_path):
    """Only the capacity refusal is soft — any other create failure must still
    propagate so a real defect is not silently swallowed."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawn_f.db'}"

    async def run():
        await init_db(db_url=db_url)
        import pytest
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        async with db_session() as db:
            db.add(Automation(id="pm-classify"))

        async def boom(**kw):
            raise ValueError("cwd does not exist: /nope")

        ctx = AutomationContext("pm-classify", create_session=boom)
        with pytest.raises(ValueError):
            await ctx.spawn_session(origin_ref="a/b#3", name="auto")
        assert not any(e["kind"] == "spawn_deferred" for e in await ctx.list_events())

    asyncio.run(run())
