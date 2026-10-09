"""GET /api/sessions/{id}/mcp, POST .../mcp/{server}/reconnect, and the layers under them."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.mcp_health.status import McpServerState
from server.sessions import session_actions as sa
from server.sessions.fleet_orchestrator import _MERGE_ALL_HOSTS, _PER_SESSION, _merge_sum_int
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client
from server.tests.fakes.fake_factory import fake_sdk_factory

SID = "11111111-1111-4111-8111-111111111111"
GH = {"name": "gh", "status": "connected", "error": None, "toolCount": 2, "scope": "user"}


def test_get_live_status():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/mcp", headers=_H)
    assert r.status_code == 200 and r.json()["servers"][0]["name"] == "gh"


def test_get_not_live_404():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        assert client.get("/api/sessions/nope/mcp", headers=_H).status_code == 404


def test_reconnect_audits_and_returns_status():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/mcp/gh/reconnect", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200 and r.json() == {"servers": [GH]}
    assert fake.reconnected == [(SID, "gh")]
    assert any(e[0] == "session.mcp_reconnect" for e in events)


def test_reconnect_sdk_error_409():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    fake.reconnect_error = "Server not found: zz"
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/mcp/zz/reconnect", headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MCP_RECONNECT_FAILED"


def test_reconnect_not_live_404_and_bad_name_400():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        assert client.post("/api/sessions/nope/mcp/gh/reconnect", headers=_H).status_code == 404
        assert client.post(f"/api/sessions/{SID}/mcp/bad name!/reconnect", headers=_H).status_code == 400


def test_routes_reach_real_facade_in_process():
    with patch.dict(os.environ, _ENV):
        from server.app import create_app
        from fastapi.testclient import TestClient
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch.mcp_status_for = AsyncMock(return_value=[GH])
    with patch.dict(os.environ, _ENV), TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(f"/api/sessions/{SID}/mcp", headers=_H)
    assert r.status_code == 200 and r.json() == {"servers": [GH]}


# --- orchestrator ---------------------------------------------------------

def _st(name, status):
    return McpServerState(name=name, status=status, error=None, tool_count=None, scope=None)


class _Drv:
    def __init__(self, states):
        self.last_mcp_status = states
        self.reconnected: list[str] = []

    async def mcp_status(self):
        return self.last_mcp_status

    async def aclose(self):
        pass

    async def reconnect_mcp_server(self, name):
        self.reconnected.append(name)


def _orch(**drivers):
    o = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                            usage_fn=lambda name: None)
    o._drivers.update(drivers)
    return o


@pytest.mark.asyncio
async def test_reconnect_everywhere_only_matching_status():
    a = _Drv([_st("plugin:p:gh", "needs-auth")])
    b = _Drv([_st("gh", "connected")])
    o = _orch(a=a, b=b)
    n = await o.reconnect_mcp_everywhere("gh", ["needs-auth", "failed"])
    assert n == 1 and a.reconnected == ["plugin:p:gh"] and b.reconnected == []


@pytest.mark.asyncio
async def test_orchestrator_status_and_reconnect_errors():
    d = _Drv([_st("gh", "connected")])
    o = _orch(a=d)
    assert await o.mcp_status_for("zz") is None
    assert (await o.mcp_status_for("a"))[0]["name"] == "gh"
    with pytest.raises(LookupError):
        await o.mcp_reconnect("zz", "gh")
    d.reconnect_mcp_server = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(ValueError, match="boom"):
        await o.mcp_reconnect("a", "gh")


# --- action layer ---------------------------------------------------------

@pytest.mark.asyncio
async def test_action_maps_errors():
    orch = SimpleNamespace(mcp_reconnect=AsyncMock(side_effect=LookupError("s")))
    with patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock):
        with pytest.raises(sa.ActionError) as e:
            await sa.mcp_reconnect(orch, db=object(), session_id="s", server="gh", identity="u")
        assert e.value.http == 404
        orch.mcp_reconnect = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(sa.ActionError) as e:
            await sa.mcp_reconnect(orch, db=object(), session_id="s", server="gh", identity="u")
        assert (e.value.http, e.value.code) == (409, "MCP_RECONNECT_FAILED")


# --- remote hop + fleet classification ------------------------------------

class _Client:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def call(self, method, **kw):
        self.calls.append((method, kw))
        return self.reply


@pytest.mark.asyncio
async def test_remote_reraises_error_shaped_replies():
    with pytest.raises(LookupError):
        await RemoteOrchestrator(_Client({"error": "not_live"})).mcp_reconnect("s", "gh")
    with pytest.raises(ValueError, match="nope"):
        await RemoteOrchestrator(_Client({"error": "reconnect_failed", "message": "nope"})).mcp_reconnect("s", "gh")
    assert await RemoteOrchestrator(_Client([GH])).mcp_reconnect("s", "gh") == [GH]
    assert await RemoteOrchestrator(_Client(3)).reconnect_mcp_everywhere("gh", ["failed"]) == 3


def test_fleet_classification():
    assert {"mcp_status_for", "mcp_reconnect"} <= _PER_SESSION
    assert _MERGE_ALL_HOSTS["reconnect_mcp_everywhere"] is _merge_sum_int
    assert _merge_sum_int([1, None, 2]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expect", [(LookupError("s"), LookupError), (ValueError("nope"), ValueError)])
async def test_daemon_handler_to_remote_round_trip_preserves_error_type(exc, expect):
    from fastapi import FastAPI
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, SimpleNamespace(mcp_reconnect=AsyncMock(side_effect=exc)))

    class _Loop:
        async def call(self, method, **kw):
            return await app.state.rpc[method](kw)

    with pytest.raises(expect):
        await RemoteOrchestrator(_Loop()).mcp_reconnect("s", "gh")


# --- fix round 1 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_exception_from_sdk_becomes_value_error():
    d = _Drv([_st("gh", "connected")])
    d.reconnect_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    with pytest.raises(ValueError, match="Server not found: zz"):
        await _orch(a=d).mcp_reconnect("a", "zz")


@pytest.mark.asyncio
async def test_driver_session_not_live_is_lookup_error():
    d = _Drv([])
    d.reconnect_mcp_server = AsyncMock(side_effect=RuntimeError("session not live"))
    with pytest.raises(LookupError):
        await _orch(a=d).mcp_reconnect("a", "gh")


def test_plain_exception_is_409_through_real_facade():
    with patch.dict(os.environ, _ENV):
        from server.app import create_app
        from fastapi.testclient import TestClient
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    d = _Drv([_st("gh", "connected")])
    d.reconnect_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    orch._drivers["s1"] = d
    with patch.dict(os.environ, _ENV), TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/sessions/s1/mcp/zz/reconnect", headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MCP_RECONNECT_FAILED"


@pytest.mark.asyncio
async def test_plain_exception_round_trips_to_value_error_without_warning(caplog):
    import logging
    from fastapi import FastAPI
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    d = _Drv([_st("gh", "connected")])
    d.reconnect_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _orch(a=d))

    class _Loop:
        async def call(self, method, **kw):
            return await app.state.rpc[method](kw)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="Server not found"):
            await RemoteOrchestrator(_Loop()).mcp_reconnect("a", "zz")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_reconnect_everywhere_bounds_a_hung_driver():
    import asyncio
    from server.sessions import orchestrator as orch_mod

    hung = _Drv([_st("gh", "needs-auth")])

    async def _hang(name):
        await asyncio.sleep(60)

    hung.reconnect_mcp_server = _hang
    ok = _Drv([_st("gh", "failed")])
    o = _orch(a=hung, b=ok)
    with patch.object(orch_mod, "MCP_RECONNECT_TIMEOUT_S", 0.05):
        n = await asyncio.wait_for(o.reconnect_mcp_everywhere("gh", ["needs-auth", "failed"]), 5)
    assert n == 1 and ok.reconnected == ["gh"]
