"""The Codex half of the coding-session tool surface (session tools spec §3.4).

Three layers, and the property under test differs at each:

* ``CodexSessionTools`` — the declaration equals what the Claude MCP server
  publishes (the anti-drift assertion), and running a tool never raises.
* ``CodexAgentDriver`` — a *fresh* coding thread declares the surface on
  ``thread/start``; a *resumed* one keeps its thread (its context is the work)
  and says so once in the journal, because the pinned ``thread/resume`` has no
  ``dynamicTools`` field (docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md).
* the rest of the driver — everything assistant-specific stays keyed on the
  assistant overlay, so a coding session that gained tools keeps its own
  sandbox, approval policy, marker instructions and permission cards.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from server.codex import schema
from server.sessions import events as ev
from server.sessions.tools.context import SessionToolContext
from server.tests.test_codex_driver import THREAD, Harness

RESUME_EVENT = "codex.driver.session_tools_unavailable_on_resume"


def _ctx() -> SessionToolContext:
    rec = SimpleNamespace(id="s1", name="n", repo="o/n", branch="b", cwd="/w", origin=None,
                          origin_ref=None, engine="codex", model=None,
                          permission_mode="acceptEdits", account="a", host="h",
                          active_goal=None, inferred_goal=None, kind=None)
    return SessionToolContext(session_id="s1", identity="u", kind=None, engine="codex",
                              record=lambda: rec, host="h")


# ---------------------------------------------------------------------------
# CodexSessionTools
# ---------------------------------------------------------------------------


def test_build_declares_both_tools_with_object_schemas():
    from server.sessions.tools.codex import CodexSessionTools

    st = CodexSessionTools.build(_ctx())
    assert {s.name for s in st.dynamic_tools} == {"session_context", "report_feedback"}
    for spec in st.dynamic_tools:
        assert spec.input_schema["type"] == "object"
    assert st.declares("report_feedback") and not st.declares("Bash")
    assert st.mcp_name("report_feedback") == "mcp__ccswitch__report_feedback"


def test_the_declaration_can_ride_thread_start_unchanged():
    # `ThreadStartParams.__post_init__` refuses a duplicate name and the real
    # app-server answers `-32600 duplicate dynamic tool name` for the same input,
    # so a build whose specs cannot be sent must fail here, not at `thread/start`.
    from server.sessions.tools.codex import CodexSessionTools

    st = CodexSessionTools.build(_ctx())
    wire = schema.ThreadStartParams(cwd="/w", dynamic_tools=st.dynamic_tools).to_wire()
    assert {t["name"] for t in wire["dynamicTools"]} == {"session_context",
                                                         "report_feedback"}
    assert all(t["type"] == "function" for t in wire["dynamicTools"])


@pytest.mark.asyncio
async def test_run_tool_translates_result_and_never_raises(isolated_db):
    from server.sessions.tools.codex import CodexSessionTools

    st = CodexSessionTools.build(_ctx())
    res = await st.run_tool("session_context", {})
    assert res.success and json.loads(res.content_items[0].text)["data"]["repo"] == "o/n"
    bad = await st.run_tool("nope", {})
    assert not bad.success
    # A non-object `arguments` is a tool error, never a TypeError out of a handler.
    assert not (await st.run_tool("session_context", ["not", "an", "object"])).success


@pytest.mark.asyncio
async def test_schemas_match_the_claude_server(isolated_db):
    """Anti-drift: the Codex declaration must equal what the Claude MCP server publishes."""
    from mcp.types import ListToolsRequest

    from server.sessions.tools.codex import CodexSessionTools
    from server.sessions.tools.server import build_session_server

    ctx = _ctx()
    instance = build_session_server(ctx)["instance"]
    handler = instance.request_handlers[ListToolsRequest]
    published = (await handler(ListToolsRequest(method="tools/list"))).root.tools
    reference = {t.name: t.inputSchema for t in published}
    assert reference
    declared = {s.name: dict(s.input_schema)
                for s in CodexSessionTools.build(ctx).dynamic_tools}
    assert declared == reference


def test_a_declared_spec_carries_the_tools_own_description(isolated_db):
    from server.sessions.tools.codex import CodexSessionTools
    from server.sessions.tools.tools import build_session_tools

    ctx = _ctx()
    source = {t.name: t for t in build_session_tools(ctx)}
    for spec in CodexSessionTools.build(ctx).dynamic_tools:
        assert spec.description == source[spec.name].description


# ---------------------------------------------------------------------------
# CodexAgentDriver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


@pytest.fixture
def session_tools(isolated_db):
    from server.sessions.tools.codex import CodexSessionTools

    return CodexSessionTools.build(_ctx())


@pytest.mark.asyncio
async def test_a_fresh_coding_thread_declares_the_surface(harness, session_tools):
    await harness.boot(session_tools=session_tools)
    params = harness.params(schema.METHOD_THREAD_START)
    assert {t["name"] for t in params["dynamicTools"]} == {"session_context",
                                                            "report_feedback"}


@pytest.mark.asyncio
async def test_a_coding_thread_with_tools_keeps_its_own_profile(harness, session_tools,
                                                                tmp_path):
    """The assistant's pinning must not leak: a coding session keeps its real cwd,
    its mode's sandbox/approval policy and the marker instructions. Compared against
    a driver booted with no tools at all, so a new pinned field cannot slip in."""
    other = tmp_path / "plain"
    other.mkdir()
    plain = Harness(other)
    try:
        await plain.boot()
        reference = plain.params(schema.METHOD_THREAD_START)
    finally:
        await plain.aclose()
    await harness.boot(session_tools=session_tools)
    params = dict(harness.params(schema.METHOD_THREAD_START))
    assert params.pop("dynamicTools")
    assert {k: v for k, v in params.items() if k != "cwd"} == {
        k: v for k, v in reference.items() if k != "cwd"}


@pytest.mark.asyncio
async def test_a_resumed_coding_thread_keeps_its_thread_and_logs_the_gap(
        harness, session_tools, caplog):
    with caplog.at_level(logging.INFO, logger="server.codex.driver"):
        await harness.boot(session_tools=session_tools, resume="thread-from-yesterday")
    assert len(harness.frames(schema.METHOD_THREAD_RESUME)) == 1
    assert harness.frames(schema.METHOD_THREAD_START) == []
    assert "dynamicTools" not in harness.params(schema.METHOD_THREAD_RESUME)
    assert [r.message for r in caplog.records].count(RESUME_EVENT) == 1


@pytest.mark.asyncio
async def test_no_gap_is_logged_when_there_are_no_session_tools(harness, caplog):
    with caplog.at_level(logging.INFO, logger="server.codex.driver"):
        await harness.boot(resume="thread-from-yesterday")
    assert RESUME_EVENT not in [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_a_declared_tool_call_runs_through_the_permission_gate(harness,
                                                                     session_tools):
    """`session_context` is annotated read-only, so it auto-allows and no card is
    raised — the same policy its Claude twin gets, via the same registry."""
    await harness.boot(session_tools=session_tools)
    rid = harness.server.request(schema.METHOD_ITEM_TOOL_CALL, {
        "threadId": THREAD, "turnId": "turn-1", "callId": "call-1",
        "tool": "session_context", "arguments": {}})
    reply = await harness.server.receive_reply(rid)
    assert reply["result"]["success"] is True
    body = json.loads(reply["result"]["contentItems"][0]["text"])
    assert body["data"]["sessionId"] == "s1"
    assert harness.of(ev.PermissionRequest) == []


@pytest.mark.asyncio
async def test_an_undeclared_tool_is_refused(harness, session_tools):
    await harness.boot(session_tools=session_tools)
    rid = harness.server.request(schema.METHOD_ITEM_TOOL_CALL, {
        "threadId": THREAD, "turnId": "turn-1", "callId": "call-1",
        "tool": "Bash", "arguments": {"command": "rm -rf /"}})
    reply = await harness.server.receive_reply(rid)
    assert reply["result"]["success"] is False


@pytest.mark.asyncio
async def test_a_coding_thread_without_tools_answers_no_tool_call(harness):
    """The callback stays unregistered when nothing declared a surface, so the
    protocol takes its own fail-closed path instead of reaching a driver."""
    await harness.boot()
    rid = harness.server.request(schema.METHOD_ITEM_TOOL_CALL, {
        "threadId": THREAD, "turnId": "turn-1", "callId": "call-1",
        "tool": "session_context", "arguments": {}})
    reply = await harness.server.receive_reply(rid)
    assert reply.get("result", {}).get("success") is not True


# ---------------------------------------------------------------------------
# Orchestrator seam
# ---------------------------------------------------------------------------


def test_the_orchestrator_getter_is_type_checked():
    """``_overlays`` holds whichever overlay a kind built, so the Codex factory's
    getter must refuse a Claude options dict rather than hand it to the driver."""
    from server.assistant.options_codex import CodexAssistantOverlay
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.tools.codex import CodexSessionTools

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    tools = CodexSessionTools(dynamic_tools=(), handlers={})
    assistant: Any = CodexAssistantOverlay(
        base_instructions="x", dynamic_tools=(), handlers={}, cwd="/w")
    orch._overlays = {"a": tools, "b": {"mcp_servers": {}}, "c": assistant}
    assert orch._codex_session_tools_for_session("a") is tools
    assert orch._codex_session_tools_for_session("b") is None
    assert orch._codex_session_tools_for_session("c") is None
    assert orch._codex_session_tools_for_session("missing") is None
