"""Post-start enforcement of desired_disabled_mcp (R5): a compose() racing trust_project can erase
the CLI's disabledMcpServers list; the driver re-applies it once per CLI start."""
from __future__ import annotations
import logging

import pytest

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient


def _status(client, base):
    """Status callable that reflects toggles, like the real CLI."""
    def f():
        last = {}
        for n, en in client.toggled:
            last[n] = en
        off = {n for n, en in last.items() if not en}
        return [{"name": n, "status": "disabled" if n in off else st} for n, st in base]
    return f


@pytest.fixture
def make(monkeypatch):
    monkeypatch.delenv("VIBERPIT_MCP_STATUS_ENABLED", raising=False)
    monkeypatch.delenv("VIBERPIT_MCP_SCOPE_ENABLED", raising=False)

    def _make(base, desired, **kw):
        client = FakeSDKClient([])
        client._mcp_status = _status(client, base)
        seen = []

        async def on_mcp(sid, states):
            seen.append([(s.name, s.status) for s in states])
        d = ClaudeAgentDriver(
            "s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
            sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
            identity="me", on_mcp_status=on_mcp, desired_disabled_mcp=desired, **kw)
        return d, client, seen
    return _make


@pytest.mark.asyncio
async def test_connected_server_is_toggled_off_once_and_status_reemitted(make):
    d, client, seen = make([("a", "connected"), ("b", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    assert client.toggled == [("a", False)]
    assert seen[-1] == [("a", "disabled"), ("b", "connected")]
    await d.aclose()


@pytest.mark.asyncio
async def test_already_disabled_not_toggled(make):
    d, client, _ = make([("a", "disabled")], ["a"])
    await d.start()
    await d._mcp_poll_task
    assert client.toggled == []
    await d.aclose()


@pytest.mark.asyncio
async def test_unreported_name_skipped(make):
    d, client, _ = make([("b", "connected")], ["ghost"])
    await d.start()
    await d._mcp_poll_task
    assert client.toggled == []
    await d.aclose()


@pytest.mark.asyncio
async def test_toggle_failure_is_debug_only(make, caplog):
    d, client, _ = make([("a", "connected")], ["a"])
    client.toggle_error = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG):
        await d.start()
        await d._mcp_poll_task
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    await d.aclose()


@pytest.mark.asyncio
async def test_no_enforcement_after_close_begins(make):
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    client.toggled.clear()
    d._closing = True
    d._mcp_enforced = False   # would otherwise run: only the _closing guard stops it
    await d._enforce_disabled_mcp(d._mcp_last)
    assert client.toggled == []


@pytest.mark.asyncio
async def test_kill_switch_off_no_enforcement(make, monkeypatch):
    monkeypatch.setenv("VIBERPIT_MCP_SCOPE_ENABLED", "false")
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    assert client.toggled == []
    await d.aclose()


@pytest.mark.asyncio
async def test_none_desired_is_noop(make):
    d, client, _ = make([("a", "connected")], None)
    await d.start()
    await d._mcp_poll_task
    assert client.toggled == []
    await d.aclose()


@pytest.mark.asyncio
async def test_reenforced_once_after_clear_reconnect(make):
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    client.toggled.clear()   # a fresh CLI process starts with 'a' connected again
    await d.reconnect()
    await d._mcp_poll_task
    assert client.toggled == [("a", False)]
    await d.aclose()


@pytest.mark.asyncio
async def test_user_toggle_on_survives_clear_reconnect(make):
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    await d.toggle_mcp_server("a", True)     # user overrides the repo rule
    client.toggled.clear()
    await d.reconnect()
    await d._mcp_poll_task
    assert client.toggled == []              # enforcement must not undo the user's choice
    await d.aclose()


@pytest.mark.asyncio
async def test_toggle_before_first_settle_is_not_redisabled(make):
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    d._mcp_enforced = False                  # as if the first settle had not run yet
    await d.toggle_mcp_server("a", True)
    client.toggled.clear()
    await d._enforce_disabled_mcp(d._mcp_last)
    assert client.toggled == []
    await d.aclose()


@pytest.mark.asyncio
async def test_user_toggle_off_joins_desired_set(make):
    d, client, _ = make([("a", "connected")], None)
    await d.start()
    await d._mcp_poll_task
    await d.toggle_mcp_server("a", False)
    client.toggled.clear()
    await d.reconnect()
    await d._mcp_poll_task
    assert client.toggled == [("a", False)]  # a user disable persists across a CLI restart
    await d.aclose()


@pytest.mark.asyncio
async def test_failed_toggle_leaves_desired_set_alone(make):
    d, client, _ = make([("a", "connected")], ["a"])
    await d.start()
    await d._mcp_poll_task
    client.toggle_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await d.toggle_mcp_server("a", True)
    assert list(d._desired_disabled_mcp) == ["a"]
    await d.aclose()
