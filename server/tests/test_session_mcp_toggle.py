"""POST /api/sessions/{id}/mcp/{server}/toggle and the layers under it."""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.db import db_session
from server.db.models import Session as DbSession
from server.mcp_health.status import McpServerState
from server.sessions import session_actions as sa
from server.sessions.fleet_orchestrator import _PER_SESSION
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

SID = "11111111-1111-4111-8111-111111111111"
GH = {"name": "gh", "status": "connected", "error": None, "toolCount": 2, "scope": "user"}
_KEYS = "server.api.sessions.configured_server_keys"


def _st(name, status):
    return McpServerState(name=name, status=status, error=None, tool_count=None, scope=None)


class _Drv:
    def __init__(self, states):
        self.last_mcp_status = states
        self.toggled: list[tuple] = []

    async def mcp_status(self):
        return self.last_mcp_status

    async def aclose(self):
        pass

    async def toggle_mcp_server(self, name, enabled):
        self.toggled.append((name, enabled))


def _orch(**drivers):
    o = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                            usage_fn=lambda name: None)
    o._drivers.update(drivers)
    return o


def _post(client, server, enabled=False, sid=SID):
    return client.post(f"/api/sessions/{sid}/mcp/{server}/toggle", headers=_H, json={"enabled": enabled})


# --- routes ---------------------------------------------------------------

def test_toggle_calls_driver_persists_override_and_returns_status():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["gh"]), client:
        r = _post(client, "gh", False)
    assert r.status_code == 200 and r.json() == {"servers": [GH]}
    assert fake.toggled == [(SID, "gh", False)]


def test_toggle_url_encoded_plugin_key_reaches_orchestrator_whole():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["plugin:p:s"]), client:
        r = _post(client, "plugin%3Ap%3As", True)
    assert r.status_code == 200
    assert fake.toggled == [(SID, "plugin:p:s", True)]


def test_toggle_not_live_404():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["gh"]), client:
        assert _post(client, "gh", sid="nope").status_code == 404


def test_toggle_unknown_server_400():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["other"]), client:
        r = _post(client, "gh")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "UNKNOWN_SERVER"
    assert fake.toggled == []


def test_toggle_sdk_failure_409():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    fake.toggle_error = "Server not found: gh"
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["gh"]), client:
        r = _post(client, "gh")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MCP_TOGGLE_FAILED"


def test_toggle_kill_switch_404():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_MCP_SCOPE_ENABLED": "false"}), \
            patch(_KEYS, return_value=["gh"]), client:
        r = _post(client, "gh")
    assert r.status_code == 404 and r.json() == {"error": "mcp_scope_disabled"}
    assert fake.toggled == []


def test_toggle_audited():
    client, fake = _make_client()
    fake.mcp = {SID: [GH]}
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["gh"]), client:
        _post(client, "gh", True)
        events = client.portal.call(_audit_events)
    assert [e[2] for e in events if e[0] == "session.mcp_toggle"] == [
        {"sessionId": SID, "server": "gh", "enabled": True}]


def test_toggle_plain_exception_is_409_through_real_facade():
    with patch.dict(os.environ, _ENV):
        from fastapi.testclient import TestClient
        from server.app import create_app
        orch = _orch()
        app = create_app(orchestrator=orch)
    d = _Drv([_st("gh", "connected")])
    d.toggle_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    orch._drivers["s1"] = d
    with patch.dict(os.environ, _ENV), patch(_KEYS, return_value=["zz"]), \
            TestClient(app, raise_server_exceptions=False) as c:
        r = _post(c, "zz", sid="s1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MCP_TOGGLE_FAILED"


# --- orchestrator ---------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_toggle_saves_override_after_driver_success(isolated_db):
    async with db_session() as db:
        db.add(DbSession(id="a", name="n", account_name="acct", cwd="/tmp"))
    d = _Drv([_st("gh", "disabled")])
    out = await _orch(a=d).mcp_toggle("a", "gh", False)
    assert d.toggled == [("gh", False)] and out[0]["name"] == "gh"
    async with db_session() as db:
        assert (await db.get(DbSession, "a")).mcp_overrides == {"gh": False}


@pytest.mark.asyncio
async def test_orchestrator_toggle_persists_full_plugin_key(isolated_db):
    async with db_session() as db:
        db.add(DbSession(id="a", name="n", account_name="acct", cwd="/tmp"))
    d = _Drv([_st("plugin:p:s", "connected")])
    await _orch(a=d).mcp_toggle("a", "plugin:p:s", True)
    assert d.toggled == [("plugin:p:s", True)]
    async with db_session() as db:
        assert (await db.get(DbSession, "a")).mcp_overrides == {"plugin:p:s": True}


@pytest.mark.asyncio
async def test_orchestrator_toggle_failure_saves_nothing(isolated_db):
    async with db_session() as db:
        db.add(DbSession(id="a", name="n", account_name="acct", cwd="/tmp"))
    d = _Drv([])
    d.toggle_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    with pytest.raises(ValueError, match="Server not found: zz"):
        await _orch(a=d).mcp_toggle("a", "zz", False)
    async with db_session() as db:
        assert not (await db.get(DbSession, "a")).mcp_overrides


@pytest.mark.asyncio
async def test_orchestrator_toggle_gone_row_still_returns_status(isolated_db):
    d = _Drv([_st("gh", "disabled")])
    out = await _orch(a=d).mcp_toggle("a", "gh", False)
    assert out[0]["name"] == "gh"


@pytest.mark.asyncio
async def test_orchestrator_toggle_error_mapping():
    o = _orch()
    with pytest.raises(LookupError):
        await o.mcp_toggle("zz", "gh", True)
    d = _Drv([])
    d.toggle_mcp_server = AsyncMock(side_effect=RuntimeError("session not live"))
    with pytest.raises(LookupError):
        await _orch(a=d).mcp_toggle("a", "gh", True)
    d.toggle_mcp_server = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(ValueError, match="boom"):
        await _orch(a=d).mcp_toggle("a", "gh", True)


# --- action layer / remote / fleet ---------------------------------------

@pytest.mark.asyncio
async def test_action_maps_errors():
    orch = SimpleNamespace(mcp_toggle=AsyncMock(side_effect=LookupError("s")))
    with patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock):
        with pytest.raises(sa.ActionError) as e:
            await sa.mcp_toggle(orch, db=object(), session_id="s", server="gh", enabled=True, identity="u")
        assert e.value.http == 404
        orch.mcp_toggle = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(sa.ActionError) as e:
            await sa.mcp_toggle(orch, db=object(), session_id="s", server="gh", enabled=True, identity="u")
        assert (e.value.http, e.value.code) == (409, "MCP_TOGGLE_FAILED")


class _Client:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def call(self, method, **kw):
        self.calls.append((method, kw))
        return self.reply


@pytest.mark.asyncio
async def test_remote_reraises_error_shaped_replies():
    with pytest.raises(LookupError):
        await RemoteOrchestrator(_Client({"error": "not_live"})).mcp_toggle("s", "gh", True)
    with pytest.raises(ValueError, match="nope"):
        await RemoteOrchestrator(_Client({"error": "toggle_failed", "message": "nope"})).mcp_toggle("s", "gh", True)
    c = _Client([GH])
    assert await RemoteOrchestrator(c).mcp_toggle("s", "gh", False) == [GH]
    assert c.calls == [("mcp_toggle", {"session_id": "s", "server": "gh", "enabled": False})]


def test_fleet_classification():
    assert "mcp_toggle" in _PER_SESSION


@pytest.mark.asyncio
async def test_plain_exception_round_trips_to_value_error_without_warning(caplog):
    from fastapi import FastAPI
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    d = _Drv([_st("gh", "connected")])
    d.toggle_mcp_server = AsyncMock(side_effect=Exception("Server not found: zz"))
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _orch(a=d))

    class _Loop:
        async def call(self, method, **kw):
            return await app.state.rpc[method](kw)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="Server not found"):
            await RemoteOrchestrator(_Loop()).mcp_toggle("a", "zz", False)
        with pytest.raises(LookupError):
            await RemoteOrchestrator(_Loop()).mcp_toggle("nope", "zz", False)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
