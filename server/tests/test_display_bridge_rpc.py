"""display_bridge_ensure RPC, the /info advertisement, and the probe snapshot."""
from __future__ import annotations

import asyncio

from sqlalchemy import select as sa_select
from starlette.testclient import TestClient

from server.db import db_session, init_db
from server.db.models import Host
from server.ipc.daemon import create_daemon_app
from server.ipc.display_bridge_rpc import register_display_bridge


class _FakeBridge:
    def __init__(self):
        self.calls = []

    async def ensure(self, sources, ssh_dests):
        self.calls.append((sources, ssh_dests))
        return [{"source": s, "state": "starting", "legs": ["wayland"], "since": None}
                for s in sources]

    def info(self):
        return {"accepts": ["rocky"], "active": []}


def _app(bridge):
    app = create_daemon_app()
    app.state.display_bridge = bridge
    register_display_bridge(app, bridge)
    return app


def test_rpc_roundtrip_and_info_key():
    bridge = _FakeBridge()
    app = _app(bridge)
    out = asyncio.run(app.state.rpc["display_bridge_ensure"](
        {"sources": ["rocky"], "sshDests": {"rocky": "tony@rocky"}}))
    assert out == {"bridges": [
        {"source": "rocky", "state": "starting", "legs": ["wayland"], "since": None}]}
    assert bridge.calls == [(["rocky"], {"rocky": "tony@rocky"})]
    # Malformed params never reach the bridge as anything but str lists / str maps.
    asyncio.run(app.state.rpc["display_bridge_ensure"](
        {"sources": ["zeta", 7, None], "sshDests": {"zeta": 3, "rocky": "rocky"}}))
    assert bridge.calls[-1] == (["zeta"], {"rocky": "rocky"})
    with TestClient(app) as c:
        assert c.get("/info").json()["displayBridge"] == {"accepts": ["rocky"], "active": []}


def test_rpc_without_bridge_reports_none_and_info_null():
    app = _app(None)
    out = asyncio.run(app.state.rpc["display_bridge_ensure"]({"sources": ["rocky"]}))
    assert out == {"bridges": []}
    with TestClient(app) as c:
        assert c.get("/info").json()["displayBridge"] is None


def test_probe_persists_display_bridge_snapshot(tmp_path):
    from server.sessions.host_probe import probe_all

    async def run(info, dbname):
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="https://dobby:9443"))

        async def fetch(endpoint):
            return info
        await probe_all(fetch=fetch)
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "dobby"))).scalars().one()

    snap = {"accepts": ["rocky"], "active": [{"source": "rocky", "since": 1.0,
                                              "legs": ["wayland", "x11"]}]}
    row = asyncio.run(run({"sha": "a", "protocol": 1, "displayBridge": snap}, "b1.db"))
    assert row.display_bridge == snap
    # Protocol-fenced: still recorded.
    row = asyncio.run(run({"sha": "a", "protocol": 999, "displayBridge": snap}, "b2.db"))
    assert row.healthy is False and row.display_bridge == snap
    # A daemon that doesn't advertise it (older, or bridge disabled): NULL.
    row = asyncio.run(run({"sha": "a", "protocol": 1}, "b3.db"))
    assert row.display_bridge is None
    row = asyncio.run(run({"sha": "a", "protocol": 1, "displayBridge": None}, "b4.db"))
    assert row.display_bridge is None
