"""What autopilot judges FROM: the transcript excerpt and the hand-back signal.

Both inputs are assembled in ``SessionOrchestrator`` (``_autopilot_transcript_tail``,
``_autopilot_get_context``) and consumed by the decider/composer prompts. Driven
against a bare ``__new__`` orchestrator, mirroring test_autopilot_orchestrator_seams.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from server.sessions.engine import AgentEngine
from server.sessions.events import TextDelta, ToolCall, ToolKind, UserMessage
from server.sessions.replay import ReplayWindow


def _bare_orch(events, *, status="done"):
    import server.sessions.orchestrator as orch_mod
    from server.sessions.types import SessionRecord
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._turn_restart_at = {}
    orch._records = {"s1": SessionRecord(
        id="s1", name="Plan the Windows test host", account="a",
        engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80, rows=24,
        cwd="/tmp", status=status)}
    orch._registry = None
    orch.pending_questions = lambda sid: []

    async def fake_window(session_id, *, before=None, limit=20):
        return ReplayWindow(events=events, earliest_turn=0, total_turns=1)
    orch.transcript_window = fake_window
    return orch


_ENDING = "Nothing further is actionable until you answer."


def _long_reply() -> str:
    return ("Option (a) logs on as tony at the console; option (b) builds the KVM guest. "
            * 60) + _ENDING


# --------------------------------------------------------------------------- #
# Excerpt cuts keep the ending and name themselves.                            #
#                                                                              #
# Live 2026-09-10 (session c07bbc41): four assistant replies of 2.0–3.1k chars #
# were complete on disk; the composer saw each cut at 1500 chars with no       #
# marker and nudged "your reply cut off mid-sentence" four times running.      #
# --------------------------------------------------------------------------- #

def test_long_assistant_reply_keeps_its_ending_and_marks_the_cut():
    body = _long_reply()
    half = len(body) // 2
    orch = _bare_orch([
        UserMessage(turn_id="t1", text="What are my options?"),
        TextDelta(turn_id="t1", text=body[:half]),     # replay emits one delta per
        TextDelta(turn_id="t1", text=body[half:]),     # content block — merged first
    ])
    tail = asyncio.run(orch._autopilot_transcript_tail("s1", max_chars=1500))
    assistant = [e for e in tail if e["role"] == "assistant"]
    assert len(assistant) == 1, "consecutive deltas of one reply merge into one entry"
    text = assistant[0]["text"]
    assert text.endswith(_ENDING), "the hand-off at the END of the reply must survive"
    assert "chars omitted" in text and "harness" in text
    assert len(text) <= 1500 + 160


def test_short_entries_carry_no_marker():
    orch = _bare_orch([
        UserMessage(turn_id="t1", text="go"),
        TextDelta(turn_id="t1", text="Done: 3 tests fixed."),
    ])
    tail = asyncio.run(orch._autopilot_transcript_tail("s1"))
    assert all("omitted" not in e["text"] for e in tail)


def test_tool_entries_are_cut_with_the_same_marker():
    orch = _bare_orch([
        ToolCall(turn_id="t1", tool_id="x", name="Bash", kind=ToolKind.BASH,
                 input={"command": "pytest " + "-k long_name_ " * 200}),
    ])
    tail = asyncio.run(orch._autopilot_transcript_tail("s1", tool_chars=160))
    assert tail and tail[0]["role"] == "tool"
    assert "chars omitted" in tail[0]["text"]
    assert tail[0]["text"].startswith("Bash ")


# --------------------------------------------------------------------------- #
# The hand-back reason rides into the context for the guardrail + feed.        #
# --------------------------------------------------------------------------- #

def test_get_context_carries_the_hand_back_reason_from_the_live_driver():
    orch = _bare_orch([], status="handed_back")
    orch._drivers = {"s1": SimpleNamespace(
        last_hand_back_reason="Need your decision on the 4GB memory limit")}
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None
    assert ctx.stuck["kind"] == "handed_back"
    assert ctx.stuck["hand_back_reason"] == "Need your decision on the 4GB memory limit"


def test_get_context_hand_back_without_driver_has_empty_reason():
    """After a restart there is no driver; the status still marks the hand-back."""
    orch = _bare_orch([], status="handed_back")
    orch._drivers = {}
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None
    assert ctx.stuck == {"kind": "handed_back", "hand_back_reason": ""}


def test_get_context_other_statuses_carry_no_hand_back_key():
    orch = _bare_orch([], status="done")
    orch._drivers = {}
    ctx = asyncio.run(orch._autopilot_get_context("s1"))
    assert ctx is not None and ctx.stuck == {"kind": "done"}


# --------------------------------------------------------------------------- #
# The prompts that read these inputs must know what they are looking at.      #
# --------------------------------------------------------------------------- #

_GUIDANCE = Path(__file__).resolve().parents[1] / "automations"


def test_decider_and_composer_guidance_explain_the_excerpt_marker():
    for name in ("autopilot_guidance.md", "nudge_composer_guidance.md"):
        text = (_GUIDANCE / name).read_text(encoding="utf-8")
        assert "chars omitted" in text, name
        assert "cut off" in text, name


def test_decider_guidance_names_hand_back_as_the_humans_turn():
    text = (_GUIDANCE / "autopilot_guidance.md").read_text(encoding="utf-8")
    assert "hand_back" in text or "handed back" in text
