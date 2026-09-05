"""Annotation-gated permission cards for `mcp__ccswitch__*` tools (spec §4.4,
plan Task 3.2, issue #1073).

``server.assistant.tools.annotation_for`` classifies a ccswitch MCP tool as
READ / MUTATION / DESTRUCTIVE; ``PermissionRegistry.request`` uses that
classification — not session mode — to decide whether the tool auto-allows,
honours the grant cache, or must always surface a card:

  READ         -> always auto-allowed, no card, in every mode.
  MUTATION     -> a card the first time; "always allow" grants it, and the
                  grant cache is then honoured even in "interactive" mode
                  (unlike ordinary tools, where interactive ignores grants).
  DESTRUCTIVE  -> ALWAYS a card, even in "full-auto"; "always allow" permits
                  that one call but writes no grant, so the next call asks
                  again (``always_allowed=False`` on the pending record).

``TOOL_SPECS`` has no MUTATION/DESTRUCTIVE entries yet (Task 3.3 adds the real
ones), so these tests inject temporary fake specs via monkeypatch.
"""
import asyncio

import pytest

from server.assistant.tools import ToolClass, ToolSpec, TOOL_SPECS
from server.sessions.permissions import PermissionRegistry
from server.sessions.claude_driver import ClaudeAgentDriver
import server.sessions.events as ev


@pytest.fixture
def fake_specs(monkeypatch):
    """Inject a MUTATION and a DESTRUCTIVE tool spec for the duration of a test,
    without touching the real (READ-only, for now) TOOL_SPECS registry."""
    monkeypatch.setitem(TOOL_SPECS, "archive_session",
                         ToolSpec("archive_session", ToolClass.MUTATION, "Archive session"))
    monkeypatch.setitem(TOOL_SPECS, "delete_session",
                         ToolSpec("delete_session", ToolClass.DESTRUCTIVE, "Delete session"))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# READ ccswitch tools: auto-allowed, no card, in every mode.
# ---------------------------------------------------------------------------

def test_read_ccswitch_tool_auto_allowed_no_card_in_interactive():
    reg = PermissionRegistry()

    async def run():
        return await reg.request(
            "s1", "t1", "mcp__ccswitch__list_sessions", {},
            mode="interactive", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")

    pid, fut, always_allowed = _run(run())
    assert pid is None, "a READ ccswitch tool must never surface a card"
    assert fut.result() is True


# ---------------------------------------------------------------------------
# MUTATION ccswitch tools: card first time; grant cache then honoured even in
# "interactive" mode (deliberately different from ordinary tools).
# ---------------------------------------------------------------------------

def test_mutation_ccswitch_tool_prompts_then_grant_is_honoured_in_interactive(fake_specs):
    reg = PermissionRegistry()

    async def run():
        pid, fut, always_allowed = await reg.request(
            "s1", "t1", "mcp__ccswitch__archive_session", {"id": "s9"},
            mode="interactive", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")
        assert pid is not None, "an injected MUTATION tool must prompt"
        assert always_allowed is True
        reg.resolve(pid, "always", "me")
        assert await fut is True

        pid2, fut2, _ = await reg.request(
            "s1", "t2", "mcp__ccswitch__archive_session", {"id": "s9"},
            mode="interactive", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")
        assert pid2 is None, ("a granted ccswitch mutation tool must auto-allow even in "
                               "interactive mode — the grant cache is deliberately honoured "
                               "here, unlike ordinary tools")
        assert await fut2 is True

    _run(run())


# ---------------------------------------------------------------------------
# DESTRUCTIVE ccswitch tools: ALWAYS a card (even full-auto); "always" allows
# once but never grants.
# ---------------------------------------------------------------------------

def test_destructive_ccswitch_tool_always_prompts_even_in_full_auto(fake_specs):
    reg = PermissionRegistry()

    async def run():
        return await reg.request(
            "s1", "t1", "mcp__ccswitch__delete_session", {"id": "s9"},
            mode="full-auto", denylist=frozenset(),
            allowlist=True, identity="me")

    pid, fut, always_allowed = _run(run())
    assert pid is not None, "a DESTRUCTIVE ccswitch tool must prompt even in full-auto"
    assert always_allowed is False


def test_destructive_ccswitch_tool_always_decision_does_not_grant(fake_specs):
    reg = PermissionRegistry()

    async def run():
        pid, fut, always_allowed = await reg.request(
            "s1", "t1", "mcp__ccswitch__delete_session", {"id": "s9"},
            mode="interactive", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")
        assert always_allowed is False
        reg.resolve(pid, "always", "me")
        assert await fut is True, "the ONE call resolved 'always' must still be allowed"

        pid2, fut2, always_allowed2 = await reg.request(
            "s1", "t2", "mcp__ccswitch__delete_session", {"id": "s9"},
            mode="interactive", denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")
        assert pid2 is not None, "a destructive tool must ask again — 'always' never grants it"
        assert always_allowed2 is False
        reg.resolve(pid2, "deny", "me")
        assert await fut2 is False

    _run(run())


# ---------------------------------------------------------------------------
# An unknown / not-yet-registered ccswitch tool (annotation_for returns None,
# e.g. a Task-3.3 tool not added yet) must be treated conservatively as
# mutation-like — never raise, and still gate on the grant cache.
# ---------------------------------------------------------------------------

def test_unknown_ccswitch_tool_treated_as_mutation_like():
    reg = PermissionRegistry()

    async def run():
        return await reg.request(
            "s1", "t1", "mcp__ccswitch__not_yet_registered", {},
            mode="full-auto", denylist=frozenset(),
            allowlist=True, identity="me")

    pid, fut, always_allowed = _run(run())
    assert pid is not None, "an unrecognized ccswitch tool must prompt, not crash or auto-allow"
    assert always_allowed is True


# ---------------------------------------------------------------------------
# Third-party MCP tools are unaffected: still auto-allowed in every mode.
# ---------------------------------------------------------------------------

def test_third_party_mcp_tool_still_auto_allowed_everywhere():
    reg = PermissionRegistry()

    async def run(mode):
        return await reg.request(
            "s1", "t1", "mcp__github__create_issue", {},
            mode=mode, denylist=PermissionRegistry.DANGEROUS,
            allowlist=True, identity="me")

    for mode in ("interactive", "allowlist", "auto", "full-auto"):
        pid, fut, always_allowed = _run(run(mode))
        assert pid is None, f"third-party MCP tool must auto-allow in {mode} mode"
        assert always_allowed is True


# ---------------------------------------------------------------------------
# Driver-level: an assistant-kind session is hard-denied for any tool outside
# the ccswitch MCP server, without ever touching the registry or emitting a
# PermissionRequest card.
# ---------------------------------------------------------------------------

class _ExplodingRegistry:
    """A PermissionRegistry stand-in that fails the test if .request is ever
    called — the assistant-kind hard-deny path must short-circuit before it."""
    async def request(self, *a, **k):
        raise AssertionError("registry.request must not be called for a hard-denied tool")

    def pending(self, session_id):
        return []


def _assistant_driver(session_id="sa1"):
    return ClaudeAgentDriver(
        session_id, "primary", "/tmp/r", "/tmp/cfg", mode="full-auto",
        sdk_factory=lambda *a, **k: None, registry=_ExplodingRegistry(),
        identity="me", assistant_api=object())


def test_assistant_session_kind_is_assistant():
    d = _assistant_driver()
    assert d.kind == "assistant"


def test_non_assistant_driver_kind_is_none():
    d = ClaudeAgentDriver(
        "sc1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
        sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(), identity="me")
    assert d.kind is None


def test_assistant_session_denies_non_ccswitch_tool_without_touching_registry():
    d = _assistant_driver()
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        return await d._can_use_tool("Bash", {"command": "ls"})

    from claude_agent_sdk import PermissionResultDeny
    result = _run(run())
    assert isinstance(result, PermissionResultDeny)

    seen = []
    while not out_q.empty():
        seen.append(out_q.get_nowait())
    assert not any(isinstance(e, ev.PermissionRequest) for e in seen), (
        "a hard-denied assistant tool must never surface a permission card")


def test_assistant_session_denies_third_party_mcp_tool():
    d = _assistant_driver()

    async def run():
        return await d._can_use_tool("mcp__github__create_issue", {})

    from claude_agent_sdk import PermissionResultDeny
    result = _run(run())
    assert isinstance(result, PermissionResultDeny)


def test_assistant_session_allows_ccswitch_mcp_tool_through_to_registry():
    """A ccswitch tool must NOT be hard-denied — it reaches the (real) registry,
    which auto-allows a READ tool."""
    d = ClaudeAgentDriver(
        "sa2", "primary", "/tmp/r", "/tmp/cfg", mode="full-auto",
        sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(),
        identity="me", assistant_api=object())

    async def run():
        from claude_agent_sdk import PermissionResultAllow
        result = await d._can_use_tool("mcp__ccswitch__list_sessions", {})
        assert isinstance(result, PermissionResultAllow)

    _run(run())
