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
from server.sessions.completion import HAND_BACK_TURN_NOTE
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.registry import SESSION_TOOL_SPECS
from server.sessions.tools.tools import build_session_tools, mounted_tool_names

SIGNALS = ("mark_complete", "mark_no_pr", "hand_back", "set_hold", "propose_decision",
           "share_artifact", "request_model")


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


def test_session_tool_specs_declares_all_nine_unconditionally():
    """The permission layer resolves a tool by name across every surface — a name
    missing from this table fails open into the mutation branch, so every tool the
    handlers can build must have a spec regardless of what any one context mounts.
    `set_hold` (spec 2026-09-18 §8) is the eighth and `propose_decision` (§6) the
    ninth, both mounted unconditionally with the signalling tools; `dispatch_session`
    (child-session spec §4.2) is the tenth: declared here, mounted only with
    `signalling_tools` and the child-sessions switch. `share_artifact` is the
    eleventh, mounted unconditionally with the signalling tools."""
    assert set(SESSION_TOOL_SPECS) == {"session_context", "report_feedback",
                                       "mark_complete", "mark_no_pr", "hand_back",
                                       "set_hold", "propose_decision", "request_model",
                                       "dispatch_session", "share_artifact"}


def test_signalling_tools_are_absent_when_the_engine_cannot_keep_them():
    names = {t.name for t in build_session_tools(_ctx(signalling_tools=False))}
    assert names == {"session_context", "report_feedback"}


def test_request_model_is_declared_only_when_the_engine_can_honour_it():
    on = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=True))}
    off = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=False))}
    assert "request_model" in on
    assert "request_model" not in off
    assert {"mark_complete", "mark_no_pr", "hand_back"} <= off


def test_mark_no_pr_description_carries_both_hardened_rules():
    """The system-prompt paragraph is deliberately an index now (task-8 review): the
    tool's OWN description is the single authority for the hand-back rule and the
    no-question rule, both hardened for prod session fdc6b27a (2026-09-06), where a
    session researched, wrote no code, recommended a spec, and self-certified past
    the completion bar. mark_complete already carried the no-question rule; this
    guards mark_no_pr from silently dropping either one."""
    desc = {t.name: t.description for t in build_session_tools(_ctx(signalling_tools=True))}
    no_pr = desc["mark_no_pr"].lower()
    assert "hand-back" in no_pr and "hand_back" in no_pr
    assert "never call this in the same turn as a question" in no_pr


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
    monkeypatch.setenv("VIBERPIT_SELF_MODEL_SWITCH_ENABLED", "true")

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
    monkeypatch.setenv("VIBERPIT_SELF_MODEL_SWITCH_ENABLED", "true")

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
    monkeypatch.setenv("VIBERPIT_SELF_MODEL_SWITCH_ENABLED", "false")

    orch = _bare_orchestrator(
        AgentCapabilities(durable_tool_surface=True, self_model_switch=True))
    await orch._setup_session_overlay("s1", identity="u@x", engine="claude_code")

    ctx = captured["ctx"]
    assert ctx.signalling_tools is True
    assert ctx.self_model_switch is False


def test_every_signal_description_names_the_three_ways_a_turn_ends():
    """The distinction Tony asked for (2026-09-08): whichever signal the model is
    looking at, it sees the other two and what each does to the session."""
    desc = {t.name: t.description.lower()
            for t in build_session_tools(_ctx(signalling_tools=True))}
    for name in ("mark_complete", "mark_no_pr", "hand_back"):
        d = desc[name]
        assert "one of three ways" in d, name
        assert "mark_complete" in d and "mark_no_pr" in d and "hand_back" in d, name
    assert "may be archived" in desc["mark_complete"]
    assert "not a hand-back" in desc["hand_back"]
    assert "same message as your question" in desc["hand_back"]


def test_hand_back_is_mounted_regardless_of_self_model_switch():
    for sms in (False, True):
        names = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=sms))}
        assert "hand_back" in names
    assert "hand_back" not in {t.name for t in build_session_tools(_ctx(signalling_tools=False))}


@pytest.mark.asyncio
async def test_hand_back_acknowledges_with_the_trimmed_reason():
    result = await _call(_ctx(signalling_tools=True), "hand_back", {"reason": "  scope decision "})
    assert result.get("is_error") is not True
    assert _payload(result) == {"acknowledged": True, "reason": "scope decision",
                                "note": HAND_BACK_TURN_NOTE}


@pytest.mark.asyncio
async def test_hand_back_rejects_a_missing_reason_without_a_side_effect():
    result = await _call(_ctx(signalling_tools=True), "hand_back", {})
    assert result["is_error"] is True
    assert "reason" in result["content"][0]["text"]


def test_hand_back_schema_requires_reason():
    tool = next(t for t in build_session_tools(_ctx(signalling_tools=True)) if t.name == "hand_back")
    assert tool.input_schema["required"] == ["reason"]
    assert tool.input_schema["properties"]["reason"]["type"] == "string"


# ---- hand_back tells the session what the user will actually see -------------------
#
# Prod 2026-10-02 (session 6ab5c602): the session wrote five options INSIDE its
# thinking, called hand_back with a one-sentence reason, got `{"acknowledged": true}`
# back, and closed with "the five options above". The user saw a Thinking row and no
# options. Nothing in the loop could have told the session that thinking is invisible
# — so the acknowledgement now says what reached the user.

async def _call_with_prose(prose, args=None):
    async def signal_prose(stem: str):
        assert stem == "hand_back"
        return prose
    ctx = _ctx(signalling_tools=True, signal_prose=signal_prose)
    return _payload(await _call(ctx, "hand_back", args or {"reason": "Pick one of the options"}))


@pytest.mark.asyncio
async def test_hand_back_warns_when_no_prose_preceded_it_in_its_message():
    data = await _call_with_prose("")
    assert data["acknowledged"] is True and data["reason"] == "Pick one of the options"
    seen = data["userSees"]
    assert seen["proseChars"] == 0
    note = seen["note"].lower()
    assert "only" in note and "`reason`" in note
    assert "thinking is never shown" in note
    assert "write them out" in note


@pytest.mark.asyncio
async def test_hand_back_reports_the_prose_the_user_sees_beside_the_reason():
    data = await _call_with_prose("Everything shipped. Options:\n1. **Update graphy**\n2. **Verify**")
    seen = data["userSees"]
    assert seen["proseChars"] == len("Everything shipped. Options:\n1. **Update graphy**\n2. **Verify**")
    assert "thinking is never shown" in seen["note"].lower()
    assert "write them out" not in seen["note"].lower()


@pytest.mark.asyncio
async def test_hand_back_stays_a_bare_acknowledgement_when_the_prose_is_unknowable():
    """No probe bound (Codex surface, a test double) or the translator never saw the
    call: the harness must not GUESS that nothing was shown — a false "write it out"
    would make every hand-back repeat itself."""
    data = await _call_with_prose(None)
    assert "userSees" not in data
    bare = _payload(await _call(_ctx(signalling_tools=True), "hand_back", {"reason": "x"}))
    assert bare == {"acknowledged": True, "reason": "x", "note": HAND_BACK_TURN_NOTE}


@pytest.mark.asyncio
async def test_a_failing_prose_probe_never_fails_the_hand_back():
    async def boom(stem: str):
        raise RuntimeError("driver gone")
    ctx = _ctx(signalling_tools=True, signal_prose=boom)
    result = await _call(ctx, "hand_back", {"reason": "still a hand-back"})
    assert result.get("is_error") is not True
    assert _payload(result) == {"acknowledged": True, "reason": "still a hand-back",
                                "note": HAND_BACK_TURN_NOTE}


def test_hand_back_description_says_thinking_is_invisible_and_where_the_question_goes():
    desc = next(t.description for t in build_session_tools(_ctx(signalling_tools=True))
                if t.name == "hand_back").lower()
    assert "thinking is never shown" in desc
    assert "every option" in desc


# ── The turn contract travels with the acknowledgement (#2647) ─────────────────────
#
# Prod 2026-10-02 (session bfc8ccbf): the session called hand_back and then kept
# working in the same turn for 13+ minutes — nothing told it the hand-back lands
# only when the turn ends. The result now says so, every time, and so does the
# description; `request_model` next to it says "keep working", so the contrast has
# to be explicit.

def test_hand_back_result_and_description_say_to_finish_the_message():
    from server.sessions.tools.tools import build_session_tools
    desc = {t.name: t.description for t in build_session_tools(_ctx(signalling_tools=True))}
    low = HAND_BACK_TURN_NOTE.lower()
    assert "turn ends" in low and "finish this message" in low
    assert HAND_BACK_TURN_NOTE in desc["hand_back"]
    # `request_model`'s "never wait for the switch — keep working" must not bleed
    # into this tool's contract: the only "keep working" here is STATUS_ONLY_CLAUSE's
    # "if the objective is not met, keep working" — a different sentence.
    assert "never wait" not in desc["hand_back"].lower()


@pytest.mark.asyncio
async def test_hand_back_result_carries_the_turn_note_beside_user_sees():
    data = await _call_with_prose("Options:\n1. A\n2. B")
    assert data["note"] == HAND_BACK_TURN_NOTE
    assert "userSees" in data
