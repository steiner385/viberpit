"""Autopilot nudges are built from the session's CURRENT state (#2928).

Three field reports: a resumed session kept being told to resume after a daemon
restart; hard-rule escalations handed a human stale PR facts; and the composer asked
a marker-only session for a `mark_complete` tool it did not have. Plus the drift
guard: every tool a prompt template names must exist in the session tool registry.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.automations.autopilot_policy import Decision, strip_restart_notices
from server.automations.completion_bar import PrFact, PrFacts
from server.sessions.completion import COMPLETION_MARKER
from server.sessions.events import TextDelta, ToolCall, ToolKind, UserMessage
from server.sessions.tools.instruction import finish_signal_instruction
from server.sessions.tools.registry import SESSION_TOOL_SPECS, mounted_names
from server.tests.test_autopilot_inputs import _bare_orch
from server.tests.test_autopilot_loop import FakeCtx, _loop, _repo_ctx

_AUTOMATIONS = Path(__file__).resolve().parents[1] / "automations"


# --------------------------------------------------------------------------- #
# Restart notices are history once the session has resumed.                   #
# --------------------------------------------------------------------------- #

def test_strip_restart_notices_drops_a_pure_notice_and_keeps_real_content():
    notice = "Your turn was interrupted. Please continue."
    tail = [
        {"role": "user", "text": notice},
        {"role": "assistant", "text": "Resuming: editing parser.py"},
        {"role": "user", "text": f"{notice}\nAlso fix the lint error."},
        {"role": "assistant", "text": notice},  # only user-role harness text is cut
    ]
    assert strip_restart_notices(tail, (notice,)) == [
        {"role": "assistant", "text": "Resuming: editing parser.py"},
        {"role": "user", "text": "Also fix the lint error."},
        {"role": "assistant", "text": notice},
    ]


def _resumed_events():
    from server.sessions.orchestrator import RESUME_NUDGE
    return [
        UserMessage(turn_id="t2", text=RESUME_NUDGE),
        TextDelta(turn_id="t2", text="Continuing the parser refactor."),
        ToolCall(turn_id="t2", tool_id="x", name="Edit", kind=ToolKind.EDIT,
                 input={"file_path": "parser.py"}),
    ]


def test_resumed_session_context_carries_no_restart_instruction():
    orch = _bare_orch(_resumed_events(), status="idle")
    orch._drivers = {}
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None and ctx.interrupted_by_restart is False
    assert not any("daemon restart" in e["text"] for e in ctx.transcript_tail)
    assert any("parser refactor" in e["text"] for e in ctx.transcript_tail)


def test_still_interrupted_session_keeps_the_restart_notice():
    orch = _bare_orch(_resumed_events()[:1], status="idle")
    orch._drivers = {}
    orch._records["s1"].interrupted_by_restart = True
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None and ctx.interrupted_by_restart is True
    assert any("daemon restart" in e["text"] for e in ctx.transcript_tail)


def test_composer_and_decider_prompts_carry_the_restart_state():
    from server.automations.autopilot_decider import _build_prompt as decider_prompt
    from server.automations.nudge_composer import _build_prompt as composer_prompt
    ctx = _repo_ctx(status="idle")
    ctx.interrupted_by_restart = False
    ctx.finish_signal = "call the `mark_complete` tool"
    composed = json.loads(composer_prompt(
        ctx, intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False).split("\n\nWrite")[0])
    decided = json.loads(decider_prompt(ctx).split("\n\nDecide")[0])
    assert composed["interrupted_by_restart"] is False
    assert composed["finish_signal"] == "call the `mark_complete` tool"
    assert decided["interrupted_by_restart"] is False


# --------------------------------------------------------------------------- #
# Only tools the session actually has.                                         #
# --------------------------------------------------------------------------- #

def test_marker_session_finish_signal_names_no_tool():
    text = finish_signal_instruction(signalling_tools=False, mcp_namespaced=True)
    assert COMPLETION_MARKER in text
    assert not any(name in text for name in SESSION_TOOL_SPECS)


@pytest.mark.parametrize("namespaced", [True, False])
def test_tool_session_finish_signal_names_a_mounted_tool(namespaced):
    text = finish_signal_instruction(signalling_tools=True, mcp_namespaced=namespaced)
    mounted = mounted_names(signalling_tools=True, self_model_switch=False)
    named = re.findall(r"`(?:mcp__\w+?__)?([a-z_]+)`", text)
    assert named and all(n in mounted for n in named)
    assert ("mcp__" in text) is namespaced


def test_context_without_signalling_tools_gets_the_marker():
    """A bare orchestrator has no engine registry — unknown capabilities are the
    marker side, exactly as the session overlay treats them."""
    orch = _bare_orch([], status="done")
    orch._drivers = {}
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None and ctx.finish_signal is not None
    assert COMPLETION_MARKER in ctx.finish_signal
    assert "mark_complete" not in ctx.finish_signal


def test_context_with_signalling_tools_gets_the_tool(monkeypatch):
    orch = _bare_orch([], status="done")
    orch._drivers = {}
    orch._engine_registry = SimpleNamespace(
        capabilities_for=lambda engine: SimpleNamespace(durable_tool_surface=True))
    monkeypatch.setattr("server.config.get_settings",
                        lambda: SimpleNamespace(session_tools_enabled=True))
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None and "mark_complete" in (ctx.finish_signal or "")


_SNAKE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_CALL = re.compile(r"\bcall(?:s|ing)?\s+(?:the\s+)?`([a-z_]+)`", re.IGNORECASE)


def _prompt_vocabulary() -> set[str]:
    """Every name a template may legitimately backtick: the payload keys both
    prompts send (and their nested keys), the statuses it judges, plus the
    registered session tools."""
    from server.automations.autopilot_policy import ACTIONABLE_STATUSES
    from server.automations.autopilot_decider import _build_prompt as decider_prompt
    from server.automations.nudge_composer import _build_prompt as composer_prompt
    ctx = _repo_ctx(status="idle", facts=PrFacts(has_repo=True, prs=(
        PrFact(number=1, state="open", is_draft=False, auto_merge_enabled=True, url="u"),)))
    keys: set[str] = set()

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(k)
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(json.loads(decider_prompt(ctx).split("\n\nDecide")[0]))
    walk(json.loads(composer_prompt(
        ctx, intent_hint=None, verdict_reason=None, verdict_remedy=None, recent_nudges=[],
        rich_tail=[], facts_verified=False).split("\n\nWrite")[0]))
    stuck_and_fleet = {"hand_back_reason", "suspected_complete", "same_repo_sessions",
                       "other_active_sessions", "recently_archived", "answer_index"}
    return keys | stuck_and_fleet | ACTIONABLE_STATUSES | set(SESSION_TOOL_SPECS)


@pytest.mark.parametrize("template", ["autopilot_guidance.md", "nudge_composer_guidance.md"])
def test_templates_name_only_registered_tools_or_payload_fields(template):
    """Drift guard: a snake_case name a template backticks is either a field the
    prompt actually sends or a registered session tool. A renamed/removed tool (or
    an invented one) fails here instead of reaching a session as a dead ask."""
    text = (_AUTOMATIONS / template).read_text(encoding="utf-8")
    unknown = set(_SNAKE.findall(text)) - _prompt_vocabulary()
    assert not unknown, f"{template} names unregistered tools/fields: {sorted(unknown)}"


@pytest.mark.parametrize("template", ["autopilot_guidance.md", "nudge_composer_guidance.md"])
def test_templates_never_tell_a_session_to_call_a_tool_by_name(template):
    """Which finishing tool a session has differs per engine and kill switch, so a
    template may not hard-code one: it routes through `finish_signal` (#2928)."""
    text = (_AUTOMATIONS / template).read_text(encoding="utf-8")
    assert _CALL.findall(text) == []


def test_drift_guard_can_fail():
    """The guard must reject an unregistered tool name (conformance guards must
    prove they can fail)."""
    assert "mark_finished" not in _prompt_vocabulary()
    assert _CALL.findall("Direct the session to call `mark_complete`.") == ["mark_complete"]


# --------------------------------------------------------------------------- #
# Escalation reports fresh PR facts.                                           #
# --------------------------------------------------------------------------- #

def _armed():
    return PrFacts(has_repo=True, prs=(PrFact(
        number=7, state="open", is_draft=False, auto_merge_enabled=True,
        url="u", checks="passing"),))


@pytest.mark.asyncio
async def test_loop_escalation_refreshes_pr_facts_once_per_episode():
    ctx = FakeCtx()
    calls = []

    async def refresh(sid):
        calls.append(sid)
        return _armed()

    async def get_context(sid):
        # Stale cache: no PR. The hard loop rule escalates before _verify_facts.
        return _repo_ctx(sid, status="idle", ledger={})

    loop = _loop(ctx, refresh_facts=refresh)
    loop._get_context = get_context
    from server.automations.autopilot_policy import fingerprint
    ctx.states["s1"] = {"accounting_version": 1, "clock_version": 1,
                        "last_fingerprint": fingerprint(_repo_ctx(status="idle")),
                        "repeat_count": 3}
    await loop.tick()
    await loop.tick()
    escalations = [s for level, s, kind in ctx.emits if kind == "escalated"]
    assert len(escalations) == 1
    assert "verified on GitHub: PR #7 open (auto-merge armed)" in escalations[0]
    assert calls == ["s1"], "deduped re-escalation must not hit GitHub again"


@pytest.mark.asyncio
async def test_escalation_says_when_facts_could_not_be_refreshed():
    ctx = FakeCtx()

    async def refresh(sid):
        return None

    loop = _loop(ctx, refresh_facts=refresh)

    async def get_context(sid):
        return _repo_ctx(sid, status="idle")
    loop._get_context = get_context

    async def decide(dc):
        return Decision(action="escalate", reason="unsure")
    loop._decide_fn = decide
    await loop.tick()
    escalations = [s for level, s, kind in ctx.emits if kind == "escalated"]
    assert escalations and "could not be refreshed" in escalations[0]


@pytest.mark.asyncio
async def test_hand_back_escalation_never_refreshes():
    ctx = FakeCtx()
    calls = []

    async def refresh(sid):
        calls.append(sid)
        return _armed()

    loop = _loop(ctx, refresh_facts=refresh)

    async def get_context(sid):
        return _repo_ctx(sid, status="handed_back")
    loop._get_context = get_context
    await loop.tick()
    assert calls == []
    assert any(kind == "escalated" for _, _, kind in ctx.emits)


# --------------------------------------------------------------------------- #
# Deference is neutral; identical nudges are never re-sent.                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_deference_never_escalates_or_counts():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="keep going"))

    async def get_context(sid):
        dc = _repo_ctx(sid, status="idle")
        dc.seconds_since_human = 5.0
        return dc
    loop._get_context = get_context
    for _ in range(6):
        await loop.tick()
    assert loop._applied == []
    assert not any(kind == "escalated" for _, _, kind in ctx.emits)
    assert ctx.states["s1"].get("repeat_count", 0) == 0


@pytest.mark.asyncio
async def test_identical_uncomposed_nudge_is_not_resent():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="Rerun pytest on test_ws.py"))
    await loop.tick()
    await loop.tick()
    assert loop._applied == [("s1", "nudge")]
