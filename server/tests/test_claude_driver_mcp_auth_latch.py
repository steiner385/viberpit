"""Claude driver: observed MCP auth failures latch needs-auth (spec 2026-10-13 "Truthful status")."""
from __future__ import annotations
import asyncio
import json

import pytest

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

REJECTED = "Sign-in rejected by the server (TOKEN_EXPIRED) — re-authenticate."


def _write_creds(cfg, token):
    (cfg / ".credentials.json").write_text(json.dumps({"mcpOAuth": {
        "aws|1": {"serverName": "aws", "serverUrl": "https://h/mcp", "accessToken": token}}}))


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBERPIT_MCP_STATUS_ENABLED", raising=False)
    _write_creds(tmp_path, "tok-A")

    def make(servers=("aws",)):
        client = FakeSDKClient([], mcp_status=lambda: [{"name": n, "status": "connected"} for n in servers])

        def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
            return client
        d = ClaudeAgentDriver("s1", "primary", "/tmp/r", str(tmp_path), mode="interactive",
                              sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        emitted: list = []
        real = d._emit
        monkeypatch.setattr(d, "_emit", lambda e: (emitted.append(e), real(e))[1])
        return d, client, emitted
    return make, tmp_path


async def _turn(d, client, *msgs):
    client._messages = [f.sys_init("sess-1"), *msgs, f.result(session_id="sess-1")]
    n = d._turns_completed
    await d.send_prompt("go")

    async def _w():
        while d._turns_completed <= n or d._turn_active:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_w(), 3.0)


def _state(d, name="aws"):
    return next(s for s in d.last_mcp_status if s.name == name)


def _rejected_call(tool_id="t1", server="aws", pid=None):
    return (f.tool_use(tool_id, f"mcp__{server}__call_aws", {}, pid=pid),
            f.user_tool_result(tool_id, [{"type": "text", "text": '{"error": "TOKEN_EXPIRED"}'}],
                               is_error=True, pid=pid))


@pytest.mark.asyncio
async def test_rejected_tool_call_latches_needs_auth_and_emits(setup):
    make, _ = setup
    d, client, emitted = make()
    await d.start()
    await d._mcp_poll_task
    assert (_state(d).status, _state(d).auth) == ("connected", "oauth")
    emitted.clear()
    await _turn(d, client, *_rejected_call())
    s = _state(d)
    assert (s.status, s.auth, s.error) == ("needs-auth", "oauth", REJECTED)
    snaps = [e for e in emitted if isinstance(e, ev.McpStatus)]
    assert snaps and any(x.name == "aws" and x.status == "needs-auth" for x in snaps[0].servers)
    assert "tok-A" not in repr(d._mcp_auth_latch)
    assert [x.status for x in await d.mcp_status()] == ["needs-auth"]     # a fresh poll keeps it
    await d.aclose()


@pytest.mark.asyncio
async def test_sub_agent_call_latches_too(setup):
    make, _ = setup
    d, client, _ = make()
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, *_rejected_call(pid="task-1"))
    assert _state(d).status == "needs-auth"
    await d.aclose()


@pytest.mark.asyncio
async def test_normalized_server_name_resolves(setup):
    make, _ = setup
    d, client, _ = make(servers=("plugin:p:aws", "gh"))
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, f.tool_use("t1", "mcp__plugin_p_aws__x", {}),
                f.user_tool_result("t1", "401 Unauthorized", is_error=True))
    assert _state(d, "plugin:p:aws").status == "needs-auth"
    assert _state(d, "plugin:p:aws").error == "Sign-in rejected by the server (401) — re-authenticate."
    assert _state(d, "gh").status == "connected"
    await d.aclose()


@pytest.mark.asyncio
async def test_ok_result_without_error_does_not_latch(setup):
    make, _ = setup
    d, client, _ = make()
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, f.tool_use("t1", "mcp__aws__x", {}),
                f.user_tool_result("t1", "Found 401 rows"))
    assert _state(d).status == "connected"
    await d.aclose()


@pytest.mark.asyncio
async def test_later_ok_result_clears_the_latch(setup):
    make, _ = setup
    d, client, emitted = make()
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, *_rejected_call())
    assert _state(d).status == "needs-auth"
    emitted.clear()
    await _turn(d, client, f.tool_use("t2", "mcp__aws__x", {}), f.user_tool_result("t2", "ok"))
    assert (_state(d).status, _state(d).error) == ("connected", None)
    assert any(isinstance(e, ev.McpStatus) for e in emitted)
    await d.aclose()


@pytest.mark.asyncio
async def test_reconnect_clears_the_latch(setup):
    make, _ = setup
    d, client, _ = make()
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, *_rejected_call())
    assert _state(d).status == "needs-auth"
    await d.reconnect_mcp_server("aws")
    assert _state(d).status == "connected"
    await d.aclose()


@pytest.mark.asyncio
async def test_token_change_clears_the_latch_at_poll(setup):
    make, cfg = setup
    d, client, _ = make()
    await d.start()
    await d._mcp_poll_task
    await _turn(d, client, *_rejected_call())
    await d._poll_mcp_status()
    assert _state(d).status == "needs-auth"          # same token: still latched
    _write_creds(cfg, "tok-B")
    await d._poll_mcp_status()
    assert (_state(d).status, _state(d).auth) == ("connected", "oauth")
    await d.aclose()


@pytest.mark.asyncio
async def test_expired_credentials_overlay_on_poll(setup):
    make, cfg = setup
    (cfg / ".credentials.json").write_text(json.dumps({"mcpOAuth": {
        "aws|1": {"serverName": "aws", "accessToken": "t", "expiresAt": 1}}}))
    d, _, _ = make()
    await d.start()
    await d._mcp_poll_task
    assert _state(d).status == "needs-auth" and _state(d).error.startswith("Sign-in expired")
    await d.aclose()
