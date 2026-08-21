"""FleetRouter maps host -> orchestrator and orders failover candidates.

The split-brain guard is the invariant under test: failover means ANOTHER
DAEMON, never an in-process orchestrator over the same database. That fallback
caused the 2026-06-23 "Ready while working" incident, and N hosts multiply the
opportunities for it.
"""
from __future__ import annotations

import asyncio

import pytest


def _router_with(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db
    from server.sessions.fleet_router import FleetRouter

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
        r = FleetRouter()
        for h in hosts:
            r.add(h.name, object())
        return r
    return go


def test_pick_orders_by_rank(tmp_path):
    from server.db.models import Host

    async def run():
        r = await _router_with(
            tmp_path, "r1.db",
            Host(name="busy", endpoint="unix:///a", healthy=True, load_avg=9.0),
            Host(name="idle", endpoint="unix:///b", healthy=True, load_avg=0.1),
        )()
        return await r.pick()

    assert asyncio.run(run())[0] == "idle"


def test_pick_honours_an_explicit_preference(tmp_path):
    """Manual override puts the chosen host first but keeps the rest as failover."""
    from server.db.models import Host

    async def run():
        r = await _router_with(
            tmp_path, "r2.db",
            Host(name="busy", endpoint="unix:///a", healthy=True, load_avg=9.0),
            Host(name="idle", endpoint="unix:///b", healthy=True, load_avg=0.1),
        )()
        return await r.pick(prefer="busy")

    order = asyncio.run(run())
    assert order[0] == "busy", "explicit choice wins"
    assert "idle" in order, "other hosts remain available for failover"


def test_pick_raises_when_no_host_is_dispatchable(tmp_path):
    from server.db.models import Host
    from server.sessions.fleet_router import NoHostAvailable

    async def run():
        r = await _router_with(tmp_path, "r3.db",
                               Host(name="down", endpoint="unix:///a", healthy=False))()
        return await r.pick()

    with pytest.raises(NoHostAvailable):
        asyncio.run(run())


def test_get_unknown_host_raises(tmp_path):
    from server.db.models import Host
    from server.sessions.fleet_router import UnknownHost

    r = asyncio.run(_router_with(tmp_path, "r4.db",
                                 Host(name="rocky", endpoint="unix:///a", healthy=True))())
    with pytest.raises(UnknownHost):
        r.get("nope")


def test_prefer_an_unknown_host_raises(tmp_path):
    """A stale UI choice must be a clear 400, not a silent fall-through to auto."""
    from server.db.models import Host
    from server.sessions.fleet_router import UnknownHost

    async def run():
        r = await _router_with(tmp_path, "r5.db",
                               Host(name="rocky", endpoint="unix:///a", healthy=True))()
        return await r.pick(prefer="ghost")

    with pytest.raises(UnknownHost):
        asyncio.run(run())


def test_registry_rows_without_an_orchestrator_are_ignored(tmp_path):
    """A host row we hold no client for cannot be dispatched to."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import FleetRouter, NoHostAvailable

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'r6.db'}")
        async with db_session() as db:
            db.add(Host(name="unwired", endpoint="unix:///a", healthy=True))
        return await FleetRouter().pick()

    with pytest.raises(NoHostAvailable):
        asyncio.run(run())


def test_router_never_exposes_an_in_process_fallback():
    """Guard against a future edit reintroducing the split-brain path."""
    import inspect

    import server.sessions.fleet_router as mod

    src = inspect.getsource(mod)
    assert "SessionOrchestrator(" not in src, (
        "FleetRouter must never construct an in-process orchestrator — that is "
        "the 2026-06-23 split-brain incident")
