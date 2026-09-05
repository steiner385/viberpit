"""Assistant launch metrics (task 4.1, spec §4.10): a minimal in-process registry with
no external dependency (verified: no prometheus_client anywhere in this repo). These
tests assert observable increments across the three real wiring points — tool calls
(server/assistant/tools.py), turn timing (server/sessions/claude_driver.py) and the
live-session gauge (same file) — not the registry's own arithmetic (that is
server/tests/test_obs_metrics.py's job, if one exists; here we exercise the wiring).

Every assertion is a DELTA around the call under test, never an absolute value: the
five metrics are process-wide module singletons (server/obs/metrics.py), shared with
every other test in the suite.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from claude_agent_sdk import TextBlock

from server.assistant.client import AssistantApi
from server.assistant.tools import build_tools
from server.obs.metrics import (
    assistant_sessions_live, assistant_tool_calls_total, assistant_ttft_ms, assistant_turn_ms,
)
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _api(handler):
    return AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x", session_id="asst-1",
                        transport=httpx.MockTransport(handler))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


# ---------------------------------------------------------------------------
# assistant_tool_calls_total{tool,class,outcome}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_increments_counter_with_tool_class_and_ok_outcome():
    def handler(req: httpx.Request):
        return httpx.Response(200, json=[])

    tools = build_tools(_api(handler))
    before = assistant_tool_calls_total.value("list_sessions", "read", "ok")
    await _tool(tools, "list_sessions").handler({})
    after = assistant_tool_calls_total.value("list_sessions", "read", "ok")
    assert after == before + 1


@pytest.mark.asyncio
async def test_failed_tool_call_increments_with_error_outcome():
    def handler(req: httpx.Request):
        return httpx.Response(500, json={"detail": "boom"})

    tools = build_tools(_api(handler))
    before = assistant_tool_calls_total.value("list_sessions", "read", "error")
    await _tool(tools, "list_sessions").handler({})
    after = assistant_tool_calls_total.value("list_sessions", "read", "error")
    assert after == before + 1


# ---------------------------------------------------------------------------
# assistant_ttft_ms / assistant_turn_ms — driver-side, assistant-kind only
# ---------------------------------------------------------------------------


def _assistant_driver(client) -> ClaudeAgentDriver:
    """A driver with a non-None assistant_api so ``.kind`` resolves to ASSISTANT_KIND
    (the sole signal — see ClaudeAgentDriver.kind). The api object is never actually
    called by these tests (no tool call happens), so any object with an async
    ``aclose`` will do; a real AssistantApi keeps this honest."""
    api = _api(lambda req: httpx.Response(200, json=[]))
    return ClaudeAgentDriver("asst-metrics", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                             identity="me", assistant_api=api)


def test_assistant_turn_observes_ttft_and_turn_duration():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"), f.stream_text("Hello ", uuid="t1"),
            f.stream_text("there", uuid="t1"), f.assistant(TextBlock(text="Hello there"), uuid="t1"),
            f.result(session_id="sess-1", uuid="t1")]
    client = FakeSDKClient(msgs)

    async def run():
        d = _assistant_driver(client)
        await d.start()
        ttft_count_before = assistant_ttft_ms.count
        turn_count_before = assistant_turn_ms.count
        await d.send_prompt("hi")
        if d._pump:
            await d._pump
        await d.aclose()
        return ttft_count_before, turn_count_before

    ttft_before, turn_before = asyncio.run(run())
    assert assistant_ttft_ms.count == ttft_before + 1
    assert assistant_turn_ms.count == turn_before + 1


def test_coding_session_turn_does_not_observe_assistant_histograms():
    """A driver with no assistant_api (an ordinary coding session) must never feed
    the assistant-only histograms — the gate is is_assistant_kind(self.kind), not
    "any turn that happens to run"."""
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]
    client = FakeSDKClient(msgs)

    async def run():
        d = ClaudeAgentDriver("coding-metrics", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                              identity="me")
        await d.start()
        ttft_before = assistant_ttft_ms.count
        turn_before = assistant_turn_ms.count
        await d.send_prompt("hi")
        if d._pump:
            await d._pump
        await d.aclose()
        return ttft_before, turn_before

    ttft_before, turn_before = asyncio.run(run())
    assert assistant_ttft_ms.count == ttft_before
    assert assistant_turn_ms.count == turn_before


# ---------------------------------------------------------------------------
# assistant_sessions_live — inc on construction, dec (once, balanced) on aclose
# ---------------------------------------------------------------------------


def test_assistant_sessions_live_gauge_balances_across_driver_lifecycle():
    client = FakeSDKClient([])

    async def run():
        before = assistant_sessions_live.value
        d = _assistant_driver(client)
        live_after_construct = assistant_sessions_live.value
        await d.start()
        await asyncio.sleep(0.01)
        await d.aclose()
        live_after_close = assistant_sessions_live.value
        return before, live_after_construct, live_after_close

    before, after_construct, after_close = asyncio.run(run())
    assert after_construct == before + 1
    assert after_close == before


def test_coding_session_never_touches_the_live_gauge():
    client = FakeSDKClient([])

    async def run():
        before = assistant_sessions_live.value
        d = ClaudeAgentDriver("coding-gauge", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                              identity="me")
        await d.start()
        await asyncio.sleep(0.01)
        await d.aclose()
        return before

    before = asyncio.run(run())
    assert assistant_sessions_live.value == before
