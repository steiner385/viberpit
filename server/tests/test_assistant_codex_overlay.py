"""The Codex assistant overlay (#1321/#1290) — `server/assistant/options_codex.py`.

These tests are about the *boundary*, not the plumbing. The spike
(docs/superpowers/spikes/2026-09-06-assistant-codex-findings.md) established that
Codex cannot remove its built-in tools, so the drawer's safety on Codex rests on
four properties that are each one edit away from silently inverting:

1. every ccswitch tool is host-executed (`dynamicTools` + `item/tool/call`),
2. every built-in approval is DECLINED, with the v2 `decline` word, not the
   legacy `denied` one that reads like a deny and is an error,
3. the thread starts in a directory Codex reads no instructions from, and
4. the tool table is DERIVED from `build_tools`, never restated.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from ccswitch.engines import AgentEngine
from server.assistant import options_codex as oc
from server.assistant.options import assistant_options, assistant_overlay_for
from server.assistant.prompt import (ASSISTANT_SYSTEM_PROMPT,
                                     OPERATOR_TOOLS_DISABLED_LINE,
                                     OPERATOR_TOOLS_ENABLED_LINE)
from server.assistant.tools import MCP_PREFIX, TOOL_SPECS, build_tools
from server.codex import schema


class _Api:
    """The AssistantApi seam every tool handler closes over."""

    identity = "user@example.com"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, path: str, **_: Any) -> Any:
        self.calls.append(path)
        return []


def _api() -> Any:
    """The AssistantApi seam, duck-typed. Returned as `Any` so the fake does not
    have to inherit a real httpx-backed client just to satisfy the annotation."""
    return _Api()

@pytest.fixture
def overlay(tmp_path, monkeypatch) -> oc.CodexAssistantOverlay:
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    return oc.assistant_codex_options(api=_api(), model="gpt-5.6-sol")


# ---------------------------------------------------------------------------
# The tool surface is derived, not restated
# ---------------------------------------------------------------------------


def test_every_declared_tool_is_a_real_ccswitch_tool(overlay):
    declared = {spec.name for spec in overlay.dynamic_tools}
    assert declared, "the overlay declared no tools at all"
    assert declared <= set(TOOL_SPECS)


def test_the_declared_tools_are_exactly_the_claude_tool_table(overlay):
    # The anti-drift assertion. `build_tools` is the ONE tool table; a Codex-only
    # list would go stale the first time a tool is added, and the staleness would
    # be invisible (the drawer would simply be missing a tool on one engine).
    assert {spec.name for spec in overlay.dynamic_tools} == {
        t.name for t in build_tools(_Api())
    }


def test_a_declared_spec_carries_the_tools_own_description(overlay):
    source = {t.name: t for t in build_tools(_Api())}
    for spec in overlay.dynamic_tools:
        assert spec.description == source[spec.name].description


@pytest.mark.asyncio
async def test_every_declared_schema_matches_what_the_claude_server_publishes(overlay):
    """The anti-drift assertion for argument schemas.

    Roughly half the ccswitch tools use the SDK's shorthand (`{"id": str}`), which
    `create_sdk_mcp_server` expands at build time and Codex cannot. Comparing
    against the schemas the real MCP server publishes is what proves the Codex
    expansion is the SAME expansion, rather than a lookalike that would give the
    two engines different tool contracts.
    """
    from mcp.types import ListToolsRequest

    from server.assistant.server import build_server

    instance = build_server(_Api())["instance"]
    handler = instance.request_handlers[ListToolsRequest]
    published = (await handler(ListToolsRequest(method="tools/list"))).root.tools
    reference = {t.name: t.inputSchema for t in published}
    assert reference
    for spec in overlay.dynamic_tools:
        assert dict(spec.input_schema) == reference[spec.name], spec.name


def test_a_shorthand_schema_is_expanded_rather_than_declared_verbatim(overlay):
    # `get_session` is declared as `{"id": str}`. A python `type` on the wire is
    # not merely unserializable — it is a tool the model can never call.
    by_name = {spec.name: spec for spec in overlay.dynamic_tools}
    assert by_name["get_session"].input_schema == {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


def test_a_spec_serializes_as_a_function_dynamic_tool(overlay):
    wire = overlay.dynamic_tools[0].to_wire()
    assert wire["type"] == "function"
    assert set(wire) == {"type", "name", "description", "inputSchema"}


def test_operator_tools_change_both_the_table_and_the_prompt_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    off = oc.assistant_codex_options(api=_api(), model="m", operator=False)
    on = oc.assistant_codex_options(api=_api(), model="m", operator=True)
    assert {s.name for s in off.dynamic_tools} < {s.name for s in on.dynamic_tools}
    assert off.base_instructions.endswith(OPERATOR_TOOLS_DISABLED_LINE)
    assert on.base_instructions.endswith(OPERATOR_TOOLS_ENABLED_LINE)


def test_base_instructions_replace_the_agent_prompt(overlay):
    assert overlay.base_instructions.startswith(ASSISTANT_SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# thread/start params — the boundary, on the wire
# ---------------------------------------------------------------------------


def test_thread_start_pins_a_read_only_untrusted_thread(overlay):
    wire = overlay.thread_start_params().to_wire()
    assert wire["sandbox"] == "read-only"
    assert wire["approvalPolicy"] == "untrusted"


def test_thread_start_declares_the_tools_and_replaces_the_instructions(overlay):
    wire = overlay.thread_start_params().to_wire()
    assert len(wire["dynamicTools"]) == len(overlay.dynamic_tools)
    assert wire["baseInstructions"] == overlay.base_instructions
    # `developerInstructions` APPENDS to the app-server's own agent prompt; the
    # drawer replaces it. Sending both is refused by the schema, but sending the
    # wrong one alone would silently make the assistant a Codex coding agent
    # wearing an assistant preamble.
    assert "developerInstructions" not in wire


def test_thread_start_never_asks_for_an_ephemeral_thread(overlay):
    # An ephemeral thread cannot be replayed (`-32600 ephemeral threads do not
    # support includeTurns`), so the drawer would lose its transcript.
    assert "ephemeral" not in overlay.thread_start_params().to_wire()


def test_the_cwd_is_outside_any_repository(overlay):
    assert oc._marker_above(overlay.cwd) is None


def test_a_cwd_that_would_inherit_agents_md_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "AGENTS.md").write_text("# leak me\n")
    with pytest.raises(oc.AssistantCodexUnsafeCwd):
        oc.neutral_cwd()


# ---------------------------------------------------------------------------
# Default-deny approvals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", [schema.METHOD_COMMAND_EXECUTION_APPROVAL,
                                    schema.METHOD_FILE_CHANGE_APPROVAL])
def test_every_builtin_approval_is_declined(method):
    assert oc.assistant_approval_decision(method).to_wire() == {"decision": "decline"}


def test_the_decline_word_is_the_v2_one_not_the_legacy_one():
    # `denied` is the legacy ReviewDecision word. It is a MALFORMED response the
    # model narrates as "approval failed … I'll retry" — it looks like a deny
    # while being an error, which is how an overlay ends up secure-looking and
    # broken. The offerable set is what proves which vocabulary this is.
    assert oc.ASSISTANT_CODEX_DECLINE in schema.COMMAND_APPROVAL_DECISIONS
    assert "denied" not in schema.COMMAND_APPROVAL_DECISIONS


# ---------------------------------------------------------------------------
# Executing a tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_tool_runs_and_returns_its_envelope(overlay):
    result = await overlay.run_tool("list_sessions", {})
    assert result.success is True
    payload = json.loads(result.content_items[0].text)
    assert payload["trust"] == "untrusted-data"


@pytest.mark.asyncio
async def test_an_undeclared_tool_is_a_tool_error_not_an_execution(overlay):
    result = await overlay.run_tool("Bash", {"command": "ls"})
    assert result.success is False
    assert "no tool named" in result.content_items[0].text


@pytest.mark.asyncio
async def test_non_object_arguments_are_a_tool_error(overlay):
    result = await overlay.run_tool("list_sessions", ["not", "an", "object"])
    assert result.success is False
    assert "must be an object" in result.content_items[0].text


@pytest.mark.asyncio
async def test_a_raising_handler_becomes_a_failed_result_not_an_exception(overlay):
    async def boom(_args):
        raise RuntimeError("kaboom")

    broken = oc.CodexAssistantOverlay(
        base_instructions="x", dynamic_tools=overlay.dynamic_tools,
        handlers={"list_sessions": boom}, cwd=overlay.cwd)
    result = await broken.run_tool("list_sessions", {})
    assert result.success is False
    # The exception TYPE, never its message: a tool error reaches the model.
    assert result.content_items[0].text == "ccswitch tool error: RuntimeError"


@pytest.mark.asyncio
async def test_a_tool_error_result_reports_success_false(overlay):
    async def failing(_args):
        return {"content": [{"type": "text", "text": "nope"}], "is_error": True}

    broken = oc.CodexAssistantOverlay(
        base_instructions="x", dynamic_tools=overlay.dynamic_tools,
        handlers={"t": failing}, cwd=overlay.cwd)
    result = await broken.run_tool("t", {})
    assert result.success is False
    assert result.content_items[0].text == "nope"


def test_the_permission_name_is_the_canonical_mcp_one(overlay):
    # This is what routes a Codex tool call into the SAME PermissionRegistry
    # branch (read auto-allows, destructive always prompts) as its Claude twin.
    assert overlay.mcp_name("archive_session") == MCP_PREFIX + "archive_session"


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------


def test_the_selector_returns_the_claude_dict_for_claude():
    overlay = assistant_overlay_for(
        AgentEngine.CLAUDE_CODE, api=_Api(), server={"type": "sdk"},
        max_turns=30, model="sonnet")
    assert overlay == assistant_options(
        server={"type": "sdk"}, max_turns=30, model="sonnet")


def test_the_selector_returns_the_codex_overlay_for_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    overlay = assistant_overlay_for(
        AgentEngine.CODEX, api=_Api(), server=None, max_turns=30, model="gpt-5.6-sol")
    assert isinstance(overlay, oc.CodexAssistantOverlay)


def test_the_claude_overlay_still_removes_the_builtins():
    # Regression fence for #1290's "preserve working Claude behavior": the Codex
    # overlay is a sibling, so nothing about the Claude one may move.
    overlay = assistant_options(server={"type": "sdk"}, max_turns=30, model="sonnet")
    assert overlay["tools"] == []
    assert "Bash" in overlay["disallowed_tools"]
    assert overlay["strict_mcp_config"] is True
