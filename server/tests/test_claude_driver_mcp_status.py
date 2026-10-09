"""Claude driver MCP live status: start + settle poll, turn-end poll, reconnect, kill switch."""
from __future__ import annotations
import asyncio
import logging

import pytest
from claude_agent_sdk import TextBlock

from server.sessions import claude_driver as cd
from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

_TURN = [f.sys_init("sess-1"), f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"),
         f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]


@pytest.fixture
def make_driver(monkeypatch):
    monkeypatch.delenv("VIBERPIT_MCP_STATUS_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_MCP_STATUS_ENABLED", raising=False)

    def make(client, **kw):
        def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
            return client
        return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                                 sdk_factory=factory, registry=PermissionRegistry(),
                                 identity="me", **kw)
    return make


async def _wait_idle(d, timeout=3.0):
    async def _w():
        while d._turn_active or d._turns_completed < 1 or (d._pump is not None and not d._pump.done()):
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_w(), timeout)


@pytest.mark.asyncio
async def test_start_emits_and_persists_status(make_driver):
    seen = []

    async def on_mcp(sid, states):
        seen.append((sid, [s.status for s in states]))
    client = FakeSDKClient([], mcp_status=[{"name": "gh", "status": "connected", "tools": [{}]}])
    d = make_driver(client, on_mcp_status=on_mcp)
    await d.start()
    await d._mcp_poll_task
    assert seen == [("s1", ["connected"])]
    assert [s.name for s in await d.mcp_status()] == ["gh"]
    await d.aclose()


@pytest.mark.asyncio
async def test_start_emits_mcp_status_event(make_driver, monkeypatch):
    client = FakeSDKClient([], mcp_status=[{"name": "gh", "status": "connected"}])
    d = make_driver(client)
    emitted = []
    real = d._emit
    monkeypatch.setattr(d, "_emit", lambda e: (emitted.append(e), real(e))[1])
    await d.start()
    await d._mcp_poll_task
    got = [e for e in emitted if isinstance(e, ev.McpStatus)]
    assert len(got) == 1 and got[0].servers[0].name == "gh"
    await d.aclose()


@pytest.mark.asyncio
async def test_settle_loop_repolls_while_pending(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 0.01)
    answers = iter([[{"name": "a", "status": "pending"}], [{"name": "a", "status": "connected"}]])
    client = FakeSDKClient([], mcp_status=lambda: next(answers))
    d = make_driver(client)
    await d.start()
    await d._mcp_poll_task
    assert [s.status for s in d._mcp_last] == ["connected"]
    await d.aclose()


@pytest.mark.asyncio
async def test_settle_loop_gives_up_after_cap(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 0.01)
    monkeypatch.setattr(cd, "MCP_SETTLE_MAX_S", 0.05)
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "pending"}])
    d = make_driver(client)
    await d.start()
    await asyncio.wait_for(d._mcp_poll_task, 1.0)
    assert [s.status for s in d._mcp_last] == ["pending"]
    await d.aclose()


@pytest.mark.asyncio
async def test_unsupported_control_request_latches_and_logs_debug(make_driver, caplog):
    caplog.set_level(logging.DEBUG)
    d = make_driver(FakeSDKClient([]))          # mcp_status=None -> get_mcp_status raises
    await d.start()
    await d._mcp_poll_task
    assert d._mcp_unsupported is True
    assert not [r for r in caplog.records
                if r.levelno >= logging.WARNING and "mcp" in r.getMessage().lower()]
    await d.aclose()


@pytest.mark.asyncio
async def test_turn_end_polls(make_driver):
    calls = []

    def answer():
        calls.append(1)
        return [{"name": "a", "status": "connected"}]
    client = FakeSDKClient(_TURN, mcp_status=answer)
    d = make_driver(client)
    await d.start()
    await d._mcp_poll_task
    n = len(calls)
    await d.send_prompt("hi")
    await _wait_idle(d)
    assert len(calls) == n + 1
    await d.aclose()


@pytest.mark.asyncio
async def test_reconnect_calls_sdk_then_repolls(make_driver):
    calls = []

    def answer():
        calls.append(1)
        return [{"name": "a", "status": "connected"}]
    client = FakeSDKClient([], mcp_status=answer)
    d = make_driver(client)
    await d.start()
    await d._mcp_poll_task
    n = len(calls)
    await d.reconnect_mcp_server("a")
    assert client.reconnected == ["a"]
    assert len(calls) == n + 1
    await d.aclose()


@pytest.mark.asyncio
async def test_reconnect_on_dead_client_raises(make_driver):
    d = make_driver(FakeSDKClient([], mcp_status=[]))
    await d.start()
    d._stream_ended = True
    with pytest.raises(RuntimeError, match="session not live"):
        await d.reconnect_mcp_server("a")
    await d.aclose()


@pytest.mark.asyncio
async def test_mcp_status_returns_cache_when_stream_ended(make_driver):
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "connected"}])
    d = make_driver(client)
    await d.start()
    await d._mcp_poll_task
    d._stream_ended = True
    assert [s.name for s in await d.mcp_status()] == ["a"]
    await d.aclose()


@pytest.mark.asyncio
async def test_close_during_settle_reaps_poll_task(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 10.0)
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "pending"}])
    d = make_driver(client)
    await d.start()
    await asyncio.sleep(0.01)
    await d.aclose()
    assert d._mcp_poll_task.done()


@pytest.mark.asyncio
async def test_kill_switch_disables_polling(make_driver, monkeypatch):
    monkeypatch.setenv("VIBERPIT_MCP_STATUS_ENABLED", "false")
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "connected"}])
    d = make_driver(client)
    await d.start()
    assert d._mcp_poll_task is None
    assert await d.mcp_status() == []
    await d.aclose()


def test_capability_and_setting_default(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("VIBERPIT_MCP_STATUS_ENABLED", raising=False)
    assert ClaudeAgentDriver.capabilities.mcp_status is True
    assert Settings().mcp_status_enabled is True
