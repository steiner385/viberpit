"""begin_drain / drain_status reachable over the daemon RPC surface."""
import httpx
import pytest
from server.ipc.serve import build_daemon
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_drain_rpcs_round_trip():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    app, _ = build_daemon(orchestrator=orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
        r = await client.post("/rpc/drain_status", json={})
        assert r.json()["result"]["state"] == "idle"

        r = await client.post("/rpc/begin_drain", json={"selfclear_sec": 999})
        assert r.json()["result"]["state"] == "ready"  # no active sessions

        r = await client.post("/rpc/drain_status", json={})
        assert r.json()["result"]["state"] == "ready"
    orch.cancel_drain()
