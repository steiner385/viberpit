"""Rebrand Phase 2a: emit one spelling, accept two — forever.

Every contract here is written INTO a persisted artifact (a session transcript, a
CLI JSONL journal, a live Codex thread's tool table) and read back out later. So
the accept side can never be retired: a marker or tool name written before the
flip must keep resolving for as long as that session's history exists. These
tests are the guard against a well-meaning Phase 7 cleanup deleting the legacy
branch along with the genuinely temporary compat scaffolding.

The pairing matters as much as either half. Phase 1c (teach agents the new
spelling) and Phase 2a (teach the edge to read it) ship together precisely
because a prompt change landing ahead of a parser change strands every running
session: its completion marker stops being recognised and the session never
resolves. Each parser below is therefore checked BOTH ways in one test.
"""
from __future__ import annotations

import asyncio

import pytest
from ccswitch import brand

from server.assistant.context import render_context_header, strip_context_header
from server.assistant.tools import TOOL_SPECS, ToolClass, ToolSpec, annotation_for
from server.sessions.completion import (
    COMPLETION_MARKER,
    NO_PR_MARKER,
    has_completion_marker,
    resolution_from_text,
    strip_markers,
)
from server.sessions.events import ToolKind, tool_kind
from server.sessions.model_request import MODEL_REQUEST_INSTRUCTION, parse_model_request
from server.sessions.permissions import PermissionRegistry

LEGACY = brand.LEGACY_SLUG
NEW = brand.SLUG


# --------------------------------------------------------------------------
# Emission: exactly one spelling reaches an agent.
# --------------------------------------------------------------------------

def test_markers_are_emitted_in_the_new_spelling_only():
    assert COMPLETION_MARKER == f"<!-- {NEW}:complete -->"
    assert NO_PR_MARKER == f"<!-- {NEW}:no-pr -->"
    assert LEGACY not in COMPLETION_MARKER
    assert LEGACY not in NO_PR_MARKER


def test_model_request_instruction_teaches_the_new_spelling_only():
    """The instruction is the emit half of the pair the parser below reads."""
    assert f"{NEW}:model=" in MODEL_REQUEST_INSTRUCTION
    assert f"{LEGACY}:model=" not in MODEL_REQUEST_INSTRUCTION


def test_context_header_is_rendered_in_the_new_spelling_only():
    rendered = render_context_header({"route": "/sessions"}, "dobby")
    assert rendered.startswith(f"<{NEW}-context ")
    assert LEGACY not in rendered


def test_mcp_prefix_is_emitted_in_the_new_spelling_only():
    assert brand.MCP_TOOL_PREFIX == f"mcp__{NEW}__"
    assert brand.MCP_SERVER_NAME == NEW


# --------------------------------------------------------------------------
# Parsing: every parser reads BOTH spellings.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [NEW, LEGACY])
def test_completion_marker_parses_in_either_spelling(slug):
    assert has_completion_marker(f"all done\n<!-- {slug}:complete -->")


@pytest.mark.parametrize("slug", [NEW, LEGACY])
def test_no_pr_marker_parses_in_either_spelling(slug):
    assert resolution_from_text(f"nothing to ship\n<!-- {slug}:no-pr -->") == "not-needed"
    assert resolution_from_text(
        f"the user cancelled this work\n<!-- {slug}:no-pr -->") == "cancelled"


@pytest.mark.parametrize("slug", [NEW, LEGACY])
def test_model_request_parses_in_either_spelling(slug):
    got = parse_model_request(f'<!-- {slug}:model=opus reason="tricky merge" -->')
    assert got == {"model": "opus", "reason": "tricky merge"}


@pytest.mark.parametrize("slug", [NEW, LEGACY])
def test_strip_markers_removes_every_marker_in_either_spelling(slug):
    text = (f"All set.\n<!-- {slug}:complete -->\n<!-- {slug}:no-pr -->\n"
            f'<!-- {slug}:model=haiku reason="trivial" -->')
    assert strip_markers(text) == "All set."


@pytest.mark.parametrize("slug", [NEW, LEGACY])
def test_context_header_is_stripped_in_either_spelling(slug):
    """A pre-rebrand assistant thread's JSONL carries `<ccswitch-context …/>`; replay
    re-parses it on every reload, so failing to match would show the operator a raw
    header line above their own prompt forever."""
    body, ctx = strip_context_header(
        f'<{slug}-context route="/sessions/abc-123" host="dobby" />\nwhat is this?')
    assert body == "what is this?"
    assert ctx == {"route": "/sessions/abc-123"}


def test_marker_match_tolerates_agent_whitespace():
    """The marker is reproduced by a language model, not by code. A session that
    never resolves is a worse failure than a lenient match."""
    assert has_completion_marker(f"done <!--{NEW}:complete-->")
    assert has_completion_marker(f"done <!--  {LEGACY}:complete  -->")


# --------------------------------------------------------------------------
# MCP namespace: the accept side gates a fail-closed permission registry.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", [brand.MCP_TOOL_PREFIX, brand.LEGACY_MCP_TOOL_PREFIX])
def test_mcp_tool_stem_and_kind_accept_either_prefix(prefix):
    assert brand.mcp_tool_stem(prefix + "list_sessions") == "list_sessions"
    assert brand.is_mcp_tool(prefix + "list_sessions")
    assert tool_kind(prefix + "archive_session") is ToolKind.APP_ACTION


def test_third_party_mcp_tools_are_still_not_ours():
    """The dual-accept widens the match by exactly one prefix, not to all of mcp__."""
    assert brand.mcp_tool_stem("mcp__github__create_issue") is None
    assert not brand.is_mcp_tool("mcp__github__create_issue")
    assert tool_kind("mcp__github__create_issue") is ToolKind.MCP
    assert annotation_for("mcp__github__list_sessions") is None


@pytest.mark.parametrize("prefix", [brand.MCP_TOOL_PREFIX, brand.LEGACY_MCP_TOOL_PREFIX])
def test_annotation_resolves_under_either_prefix(prefix):
    assert annotation_for(prefix + "list_sessions").tool_class is ToolClass.READ


@pytest.fixture
def fake_specs(monkeypatch):
    monkeypatch.setitem(TOOL_SPECS, "delete_session",
                        ToolSpec("delete_session", ToolClass.DESTRUCTIVE, "Delete session"))


def test_resumed_codex_thread_with_legacy_tool_names_still_gates_destructive(fake_specs):
    """A Codex thread started before the flip keeps the tool table it was started
    with, so ``AssistantOverlay.mcp_name`` maps its calls onto LEGACY-prefixed names
    for the whole life of that thread. The permission registry must still take the
    app-action branch for them.

    This one fails closed in the dangerous direction: if the legacy prefix stopped
    matching, the call would fall through to the generic ``mcp__`` auto-allow and a
    DESTRUCTIVE tool would run with no card at all.
    """
    reg = PermissionRegistry()
    legacy_name = brand.LEGACY_MCP_TOOL_PREFIX + "delete_session"

    async def run():
        return await reg.request(
            "s1", "t1", legacy_name, {"id": "s9"},
            mode="full-auto", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")

    pid, fut, always_allowed = asyncio.run(run())
    assert pid is not None, "a legacy-named destructive tool must still surface a card"
    assert not fut.done(), "the card must park the turn, not auto-allow"
    assert always_allowed is False, "destructive tools never offer 'always allow'"
