"""The three signalling tools (mark_complete, mark_no_pr, request_model) declared on
the session MCP surface (task-3, marker-to-tool migration). Nothing reads the tool
calls yet — that is the Translator's job in a later task; this only covers
declaration, capability gating, and the handlers' own validate-and-acknowledge
behaviour."""
from __future__ import annotations

import itertools
import json

import pytest

from server.mcp_spec import ToolClass
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.registry import SESSION_TOOL_SPECS
from server.sessions.tools.tools import build_session_tools, mounted_tool_names

SIGNALS = ("mark_complete", "mark_no_pr", "request_model")


def _ctx(**kw):
    return SessionToolContext(session_id="s", identity="u", kind="coding",
                              engine="claude_code", record=lambda: None, **kw)


def test_every_signalling_tool_is_registered_as_signal_class():
    for name in SIGNALS:
        assert SESSION_TOOL_SPECS[name].tool_class is ToolClass.SIGNAL


def test_report_feedback_stays_read_class():
    """It changes nothing a user can see; mark_complete moves a status badge.
    That difference is the entire reason SIGNAL exists."""
    assert SESSION_TOOL_SPECS["report_feedback"].tool_class is ToolClass.READ


def test_session_context_stays_read_class():
    assert SESSION_TOOL_SPECS["session_context"].tool_class is ToolClass.READ


def test_session_tool_specs_declares_all_five_unconditionally():
    """The permission layer resolves a tool by name across every surface — a name
    missing from this table fails open into the mutation branch, so every tool the
    handlers can build must have a spec regardless of what any one context mounts."""
    assert set(SESSION_TOOL_SPECS) == {"session_context", "report_feedback",
                                       "mark_complete", "mark_no_pr", "request_model"}


def test_signalling_tools_are_absent_when_the_engine_cannot_keep_them():
    names = {t.name for t in build_session_tools(_ctx(signalling_tools=False))}
    assert names == {"session_context", "report_feedback"}


def test_request_model_is_declared_only_when_the_engine_can_honour_it():
    on = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=True))}
    off = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=False))}
    assert "request_model" in on
    assert "request_model" not in off
    assert {"mark_complete", "mark_no_pr"} <= off


@pytest.mark.parametrize("signalling_tools,self_model_switch",
                         list(itertools.product([False, True], repeat=2)))
def test_mounted_tool_names_agrees_with_build_session_tools(signalling_tools, self_model_switch):
    """`mounted_tool_names` is THE predicate: `build_session_tools` builds from it and
    a later system-prompt paragraph names from it, so the two must never disagree for
    any combination of the two capability flags."""
    ctx = _ctx(signalling_tools=signalling_tools, self_model_switch=self_model_switch)
    assert mounted_tool_names(ctx) == tuple(t.name for t in build_session_tools(ctx))


async def _call(ctx, name, args):
    tool = next(t for t in build_session_tools(ctx) if t.name == name)
    return await tool.handler(args)


def _payload(result):
    return json.loads(result["content"][0]["text"])["data"]


@pytest.mark.asyncio
async def test_mark_complete_acknowledges():
    result = await _call(_ctx(signalling_tools=True), "mark_complete", {})
    assert result.get("is_error") is not True
    assert _payload(result) == {"acknowledged": True}


@pytest.mark.asyncio
async def test_mark_no_pr_echoes_the_declared_resolution():
    result = await _call(_ctx(signalling_tools=True), "mark_no_pr",
                         {"resolution": "not-needed", "note": "already satisfied"})
    assert _payload(result) == {"acknowledged": True, "resolution": "not-needed",
                                "note": "already satisfied"}


@pytest.mark.asyncio
async def test_mark_no_pr_rejects_an_unknown_resolution_without_a_side_effect():
    """`guarded` turns ToolArgError into an is_error result, so the model gets a
    corrective message and the Translator never observes a usable call."""
    result = await _call(_ctx(signalling_tools=True), "mark_no_pr", {"resolution": "bogus"})
    assert result["is_error"] is True
    assert "not-needed" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_request_model_rejects_a_missing_reason():
    result = await _call(_ctx(signalling_tools=True, self_model_switch=True),
                         "request_model", {"model": "opus"})
    assert result["is_error"] is True


@pytest.mark.asyncio
async def test_request_model_acknowledges_a_valid_request():
    result = await _call(_ctx(signalling_tools=True, self_model_switch=True),
                         "request_model", {"model": "opus", "reason": "cross-module refactor"})
    assert _payload(result) == {"acknowledged": True, "model": "opus",
                                "reason": "cross-module refactor"}


def _bare_orchestrator(engine_capabilities):
    """A partial orchestrator built the same way as
    ``test_session_overlay.test_codex_engine_registers_the_codex_surface`` — only
    the state ``_setup_session_overlay`` actually touches, plus an engine registry
    whose ``claude_code`` factory declares *engine_capabilities*."""
    from types import SimpleNamespace

    from server.sessions.engine import AgentDriverRegistry, AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._overlays = {}
    orch._records = {}
    orch._engine_registry = AgentDriverRegistry(
        {AgentEngine.CLAUDE_CODE: SimpleNamespace(capabilities=engine_capabilities)})
    return orch


@pytest.mark.asyncio
async def test_setup_session_overlay_sets_signalling_tools_from_the_engine_capability(
    monkeypatch,
):
    """The orchestrator's context build must resolve ``signalling_tools`` (and
    ``self_model_switch``) from the SELECTED engine's own capability declaration —
    never a flag set by hand — so a session only gets the tools its engine can
    keep across a resume (task 4, marker-to-tool migration)."""
    from server.sessions.engine import AgentCapabilities

    captured: dict = {}

    def fake_build_session_server(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("server.sessions.tools.server.build_session_server",
                        fake_build_session_server)
    monkeypatch.setenv("CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "true")

    orch = _bare_orchestrator(
        AgentCapabilities(durable_tool_surface=True, self_model_switch=True))
    await orch._setup_session_overlay("s1", identity="u@x", engine="claude_code")

    ctx = captured["ctx"]
    assert ctx.signalling_tools is True
    assert ctx.self_model_switch is True


@pytest.mark.asyncio
async def test_setup_session_overlay_withholds_signalling_tools_without_the_capability(
    monkeypatch,
):
    """The mirror case: an engine that declares neither capability (Codex today)
    must not get the flags, even though the context object defaults them False
    anyway — this pins that the orchestrator actually consults the capability
    rather than relying on the dataclass default."""
    from server.sessions.engine import AgentCapabilities

    captured: dict = {}

    def fake_build_session_server(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("server.sessions.tools.server.build_session_server",
                        fake_build_session_server)
    monkeypatch.setenv("CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "true")

    orch = _bare_orchestrator(AgentCapabilities())
    await orch._setup_session_overlay("s1", identity="u@x", engine="claude_code")

    ctx = captured["ctx"]
    assert ctx.signalling_tools is False
    assert ctx.self_model_switch is False


@pytest.mark.asyncio
async def test_setup_session_overlay_withholds_self_model_switch_when_settings_disable_it(
    monkeypatch,
):
    """``self_model_switch`` is an AND of the engine capability and the deployment
    setting — an engine that can honour it must still lose it when the operator
    turns the feature off."""
    from server.sessions.engine import AgentCapabilities

    captured: dict = {}

    def fake_build_session_server(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("server.sessions.tools.server.build_session_server",
                        fake_build_session_server)
    monkeypatch.setenv("CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "false")

    orch = _bare_orchestrator(
        AgentCapabilities(durable_tool_surface=True, self_model_switch=True))
    await orch._setup_session_overlay("s1", identity="u@x", engine="claude_code")

    ctx = captured["ctx"]
    assert ctx.signalling_tools is True
    assert ctx.self_model_switch is False
