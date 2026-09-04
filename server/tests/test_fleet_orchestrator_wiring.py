"""Wiring test: build_fleet_router populates one orchestrator per enabled Host row.

Uses init_db(db_url=...) for DB isolation (Ruling B).
"""
from __future__ import annotations

import asyncio


def test_router_holds_a_client_per_enabled_host(tmp_path):
    """build_fleet_router should register orchestrators for enabled hosts and skip disabled ones."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_orchestrator import build_fleet_router

    built = []

    class _FakeOrch:
        def __init__(self, ep):
            built.append(ep)

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky.ts.net:9443", enabled=True))
            db.add(Host(name="dobby", endpoint="https://dobby.ts.net:9443", enabled=True))
            db.add(Host(name="old", endpoint="https://old.ts.net:9443", enabled=False))
            await db.commit()

        import server.sessions.fleet_orchestrator as _fo
        orig = _fo._orch_for_endpoint
        try:
            _fo._orch_for_endpoint = lambda ep: _FakeOrch(ep)
            router = await build_fleet_router(primary_name="rocky",
                                              primary_orch=_FakeOrch("primary"))
        finally:
            _fo._orch_for_endpoint = orig
        return router

    router = asyncio.run(run())
    assert set(router.names()) == {"rocky", "dobby"}, "'old' disabled → must be skipped"


def test_router_primary_reuses_passed_orch(tmp_path):
    """The primary orchestrator object passed in is used as-is (not rebuilt)."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_orchestrator import build_fleet_router

    class _FakeOrch:
        def __init__(self, label):
            self.label = label

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't2.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky.ts.net:9443", enabled=True))
            await db.commit()

        primary = _FakeOrch("the-primary")
        import server.sessions.fleet_orchestrator as _fo
        orig = _fo._orch_for_endpoint
        try:
            _fo._orch_for_endpoint = lambda ep: _FakeOrch(ep)
            router = await build_fleet_router(primary_name="rocky", primary_orch=primary)
        finally:
            _fo._orch_for_endpoint = orig
        return router, primary

    router, primary = asyncio.run(run())
    # The object stored under "rocky" must be the same object we passed in
    assert router.get("rocky") is primary


def test_router_skips_disabled_hosts(tmp_path):
    """Disabled hosts must not appear in router.names() even if endpoint is reachable."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_orchestrator import build_fleet_router

    class _FakeOrch:
        def __init__(self, ep): pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't3.db'}")
        async with db_session() as db:
            db.add(Host(name="primary", endpoint="https://p.ts.net:9443", enabled=True))
            db.add(Host(name="disabled-a", endpoint="https://da.ts.net:9443", enabled=False))
            db.add(Host(name="disabled-b", endpoint="https://db.ts.net:9443", enabled=False))
            await db.commit()

        import server.sessions.fleet_orchestrator as _fo
        orig = _fo._orch_for_endpoint
        try:
            _fo._orch_for_endpoint = lambda ep: _FakeOrch(ep)
            router = await build_fleet_router(primary_name="primary",
                                              primary_orch=_FakeOrch("primary"))
        finally:
            _fo._orch_for_endpoint = orig
        return router

    router = asyncio.run(run())
    assert "disabled-a" not in router.names()
    assert "disabled-b" not in router.names()
    assert "primary" in router.names()
