"""The Translator reads the signalling MCP tool calls (spec §4).

The signal is read HERE rather than in the tool handler because only the
translator can see who called: a `Task` sub-agent's message carries a
``parent_tool_use_id``, and an SDK tool handler receives nothing but its
arguments. That is the same guard `translator.py` already applies to the text
scan, on the same field.
"""
from __future__ import annotations

import itertools
import logging

from claude_agent_sdk import TextBlock

from server.sessions.events import TurnEnd
from server.sessions.translator import Translator
from server.tests.fakes import fake_sdk as f

MARK_COMPLETE = "mcp__viberpit__mark_complete"
MARK_NO_PR = "mcp__viberpit__mark_no_pr"
REQUEST_MODEL = "mcp__viberpit__request_model"
SET_HOLD = "mcp__viberpit__set_hold"

_ids = itertools.count()


def feed_assistant_tool_call(t: Translator, name: str, args: dict, pid=None) -> list:
    return t.feed(f.tool_use(f"tc{next(_ids)}", name, args, uuid="t1", pid=pid))


def feed_assistant_text(t: Translator, text: str, pid=None) -> list:
    return t.feed(f.assistant(TextBlock(text=text), uuid="t1", parent_tool_use_id=pid))


def feed_result(t: Translator, **kw) -> TurnEnd:
    out = t.feed(f.result(uuid="t1", **kw))
    return next(e for e in out if isinstance(e, TurnEnd))


def test_a_mark_complete_call_completes_the_turn():
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    end = feed_result(t)
    assert end.outcome == "complete"


def test_a_subagent_mark_complete_does_NOT_complete_the_parent_session():
    """The guard that text scanning has at translator.py:119 and an MCP handler
    cannot reproduce: a Task sub-agent finishing ITS objective is not the session
    finishing. Regression test for the whole design of spec §4."""
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid="toolu_parent")
    end = feed_result(t)
    assert end.outcome == "success"


def test_a_tool_call_survives_a_later_message_in_the_same_turn():
    """`_turn_text` resets on every top-level assistant message, so text-based
    signalling only ever reads the LAST message. Tool state must not: a session
    calls mark_complete and then writes a closing paragraph."""
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    feed_assistant_text(t, "and here is what I did.", pid=None)
    assert feed_result(t).outcome == "complete"


def test_signal_state_clears_between_turns():
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    assert feed_result(t).outcome == "complete"
    feed_assistant_text(t, "next turn, still working", pid=None)
    assert feed_result(t).outcome == "success"


def test_mark_no_pr_sets_the_declared_resolution_and_a_tool_source():
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR,
                             {"resolution": "cancelled", "note": "user pulled it"}, pid=None)
    end = feed_result(t)
    assert end.outcome == "complete"
    assert end.pr_resolution == "cancelled"
    assert end.pr_resolution_source == "tool"
    assert end.pr_resolution_note == "user pulled it"


def test_the_no_pr_note_travels_only_with_the_tool_that_carried_it():
    """The note is the session's own account of WHY there is no PR — the Backlog
    Runner settles an ended investigation from it (spec 2026-09-18 §5). The marker
    path has no argument to carry one, so it is None rather than a guess scraped out
    of the prose, and a hand-back that outranks the resolution drops it too."""
    t = Translator()
    feed_assistant_text(t, "all set\n<!-- viberpit:no-pr -->", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "not-needed" and end.pr_resolution_note is None

    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "not-needed"}, pid=None)
    assert feed_result(t).pr_resolution_note is None, "no note given"

    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR,
                             {"resolution": "not-needed", "note": "converted to a fix: x"},
                             pid=None)
    feed_assistant_tool_call(t, "mcp__viberpit__hand_back", {"reason": "which repo?"},
                             pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.pr_resolution is None and end.pr_resolution_note is None


def test_the_tool_wins_over_a_marker_in_the_same_turn():
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "cancelled"}, pid=None)
    feed_assistant_text(t, "done\n<!-- viberpit:no-pr -->", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "cancelled", "a declaration beats an inferred token"
    assert end.pr_resolution_source == "tool"


def test_a_marker_still_works_with_no_tool_call():
    t = Translator()
    feed_assistant_text(t, "all set\n<!-- viberpit:complete -->", pid=None)
    assert feed_result(t).outcome == "complete"


def test_a_no_pr_marker_alone_reports_a_marker_source():
    t = Translator()
    feed_assistant_text(t, "the user cancelled this\n<!-- viberpit:no-pr -->", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "cancelled"
    assert end.pr_resolution_source == "marker"


def test_no_signal_at_all_leaves_the_source_unset():
    t = Translator()
    feed_assistant_text(t, "still working on it", pid=None)
    end = feed_result(t)
    assert end.pr_resolution is None
    assert end.pr_resolution_source is None


def test_the_pre_rebrand_tool_name_is_still_recognised():
    t = Translator()
    feed_assistant_tool_call(t, "mcp__viberpit__mark_complete", {}, pid=None)
    assert feed_result(t).outcome == "complete"


def test_a_malformed_mark_no_pr_is_ignored_rather_than_crashing_the_turn():
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "bogus"}, pid=None)
    end = feed_result(t)
    assert end.pr_resolution is None
    assert end.outcome == "success"


def test_request_model_carries_the_declared_tier_and_reason():
    t = Translator()
    feed_assistant_tool_call(t, REQUEST_MODEL,
                             {"model": "opus", "reason": "harder than it looked"}, pid=None)
    end = feed_result(t)
    assert end.model_request == {"model": "opus", "reason": "harder than it looked"}
    assert end.outcome == "success", "asking for a model is not finishing the work"


def test_a_malformed_request_model_is_ignored_rather_than_crashing_the_turn():
    t = Translator()
    feed_assistant_tool_call(t, REQUEST_MODEL, {"model": "gpt-9"}, pid=None)
    assert feed_result(t).model_request is None


def test_a_subagent_request_model_is_ignored():
    t = Translator()
    feed_assistant_tool_call(t, REQUEST_MODEL, {"model": "opus", "reason": "hard"},
                             pid="toolu_parent")
    assert feed_result(t).model_request is None


def test_a_subagent_mark_no_pr_does_NOT_set_a_resolution():
    """mark_no_pr is the signal with a persisted side effect, so the sub-agent guard
    matters most here: a Task deciding ITS sub-objective needs no PR must not close
    out the session's PR resolution."""
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "cancelled"}, pid="toolu_parent")
    end = feed_result(t)
    assert end.pr_resolution is None
    assert end.pr_resolution_source is None
    assert end.outcome == "success"


def test_a_signalling_tool_call_is_not_rendered_as_a_tool_card():
    """A ToolCall card for `mark_complete` is noise on top of the status change it
    causes — the same reason AskUserQuestion is skipped in the loop above it."""
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    assert not any(isinstance(e, ToolCall) for e in out)


def test_a_non_signalling_viberpit_tool_still_renders_a_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, "mcp__viberpit__session_context", {}, pid=None)
    assert any(isinstance(e, ToolCall) for e in out)


def test_a_SUBAGENT_signalling_call_DOES_render_a_card():
    """The AskUserQuestion precedent (replay.py:120): the card is suppressed only for
    the top-level call that actually took effect. A sub-agent's call did nothing, so
    it renders — that card is the only sign a human reading the transcript gets that
    the tool was misused."""
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid="toolu_parent")
    cards = [e for e in out if isinstance(e, ToolCall)]
    assert len(cards) == 1
    assert cards[0].name == MARK_COMPLETE
    assert cards[0].parent_tool_use_id == "toolu_parent"


def test_the_signal_tool_names_come_from_the_spec_registry():
    """One source of truth: the names the permission layer treats as no-card signals
    are exactly the ToolClass.SIGNAL entries in the session tool spec table, not a
    re-typed copy — and the names the TRANSCRIPT suppresses are the strictly smaller
    set the Translator actually reads as a turn signal."""
    from server.mcp_spec import ToolClass
    from server.sessions.tools.registry import (SESSION_TOOL_SPECS, SIGNAL_TOOL_NAMES,
                                                TURN_SIGNAL_NAMES)
    assert SIGNAL_TOOL_NAMES == {n for n, s in SESSION_TOOL_SPECS.items()
                                 if s.tool_class is ToolClass.SIGNAL}
    assert SIGNAL_TOOL_NAMES == {"mark_complete", "mark_no_pr", "hand_back", "set_hold",
                                 "propose_decision", "share_artifact", "request_model",
                                 "reply_to_child"}
    assert TURN_SIGNAL_NAMES < SIGNAL_TOOL_NAMES
    assert TURN_SIGNAL_NAMES == {"mark_complete", "mark_no_pr", "hand_back",
                                 "request_model"}
    # Both roadmap-writing signals stay OUT of the turn set: the card they render is
    # the transcript's only record of a lasting effect, and neither ends the turn —
    # `propose_decision` is followed by `mark_no_pr("awaiting-decision")`, which does.
    assert "set_hold" not in TURN_SIGNAL_NAMES
    assert "propose_decision" not in TURN_SIGNAL_NAMES
    # `share_artifact` stays out for the strongest reason of all: the card IS the
    # feature — suppressing it would hide exactly what the tool exists to show.
    assert "share_artifact" not in TURN_SIGNAL_NAMES


def test_a_top_level_set_hold_renders_a_card_and_ends_no_turn():
    """``set_hold`` is a SIGNAL for the permission layer but NOT a turn signal: the
    Translator has no branch for it, so suppressing its card left a session mutating
    roadmap state with no trace in the transcript at all (final-review F2). It must
    fall through to an ordinary ToolCall card and leave the turn outcome alone."""
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, SET_HOLD, {"kind": "evidence", "reason": "no repro"},
                                   pid=None)
    cards = [e for e in out if isinstance(e, ToolCall)]
    assert len(cards) == 1
    assert cards[0].name == SET_HOLD
    assert cards[0].input == {"kind": "evidence", "reason": "no repro"}
    assert feed_result(t).outcome == "success"


def test_a_propose_decision_call_renders_a_card_and_the_turn_ends_on_mark_no_pr():
    """The §6 pair, as the Translator sees it: the proposal is a lasting roadmap
    effect (a ``decision`` hold and an inbox card), so it renders like ``set_hold``
    and does NOT end the turn; the ``mark_no_pr("awaiting-decision")`` that follows
    is what resolves it — and the resolution it carries is what the Backlog Runner
    later reads back off the session row."""
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, "mcp__viberpit__propose_decision",
                                   {"options": [{"key": "a", "label": "A"},
                                                {"key": "b", "label": "B"}],
                                    "recommended_key": "a", "rationale": "why"},
                                   pid=None)
    assert [e.name for e in out if isinstance(e, ToolCall)] == [
        "mcp__viberpit__propose_decision"]
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "awaiting-decision",
                                             "note": "which archive rule?"}, pid=None)
    end = feed_result(t)
    assert (end.outcome, end.pr_resolution) == ("complete", "awaiting-decision")
    assert end.pr_resolution_source == "tool" and end.pr_resolution_note == "which archive rule?"


def test_a_failed_result_stays_failed_even_with_a_mark_complete_call():
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    assert feed_result(t, is_error=True).outcome == "failed"


def test_a_declared_resolution_that_contradicts_the_prose_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="server.sessions.translator")
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "not-needed"}, pid=None)
    feed_assistant_text(t, "the user cancelled this work\n<!-- viberpit:no-pr -->", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "not-needed", "the declaration is still what is recorded"
    assert "no_pr_resolution_disagreement" in caplog.text


def test_an_agreeing_resolution_is_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="server.sessions.translator")
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "cancelled"}, pid=None)
    feed_assistant_text(t, "the user cancelled this work\n<!-- viberpit:no-pr -->", pid=None)
    assert feed_result(t).pr_resolution == "cancelled"
    assert "no_pr_resolution_disagreement" not in caplog.text


HAND_BACK = "mcp__viberpit__hand_back"


def test_a_hand_back_call_ends_the_turn_as_hand_back_with_its_reason():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "need a decision on scope"}, pid=None)
    feed_assistant_text(t, "Per-IP or per-account?", pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.hand_back_reason == "need a decision on scope"
    assert end.to_wire()["handBackReason"] == "need a decision on scope"


def test_a_subagent_hand_back_does_NOT_hand_back_the_session():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "sub-agent question"}, pid="toolu_parent")
    end = feed_result(t)
    assert end.outcome == "success"
    assert end.hand_back_reason is None


def test_a_subagent_hand_back_still_renders_as_an_ordinary_tool_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid="toolu_parent")
    assert any(isinstance(e, ToolCall) for e in out)


def test_a_top_level_hand_back_renders_no_tool_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    assert not any(isinstance(e, ToolCall) for e in out)


def test_hand_back_beats_mark_complete_in_the_same_turn_and_logs_it(caplog):
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "one open question"}, pid=None)
    with caplog.at_level(logging.INFO, logger="server.sessions.translator"):
        end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.pr_resolution is None
    assert any("hand_back_over_completion" in r.getMessage() for r in caplog.records)


def test_hand_back_beats_mark_no_pr_and_drops_the_resolution():
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "not-needed"}, pid=None)
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "confirm before closing"}, pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.pr_resolution is None and end.pr_resolution_source is None


def test_the_hand_back_marker_in_prose_is_the_fallback():
    t = Translator()
    feed_assistant_text(
        t, 'Wire or remove?\n<!-- viberpit:hand-back reason="wire vs remove" -->', pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.hand_back_reason == "wire vs remove"


def test_the_tool_reason_wins_over_the_marker_reason():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "from the tool"}, pid=None)
    feed_assistant_text(t, '<!-- viberpit:hand-back reason="from the marker" -->', pid=None)
    assert feed_result(t).hand_back_reason == "from the tool"


def test_a_failed_result_still_wins_over_hand_back():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    end = feed_result(t, subtype="error_max_turns", is_error=True)
    assert end.outcome == "failed"


def test_a_hand_back_with_a_bad_reason_is_dropped_not_fatal():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": ""}, pid=None)
    assert feed_result(t).outcome == "success"


def test_hand_back_state_clears_between_turns():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    assert feed_result(t).outcome == "hand_back"
    feed_assistant_text(t, "continuing", pid=None)
    assert feed_result(t).outcome == "success"


def test_prose_beside_a_hand_back_call_streams_whole_and_the_closing_prose_too():
    """Regression for the 2026-10-02 report "the five options above" (session
    6ab5c602): the hypothesis was that hiding the hand_back card also hid the text
    block sharing its message, or folded it into the Thinking row. It did not — the
    transcript had no text block to show — but pin it: an assistant message shaped
    [thinking, text, hand_back] streams BOTH the thinking and the text, the call
    itself renders nothing, and the short prose after the tool result streams as well.
    """
    from claude_agent_sdk import ThinkingBlock, ToolUseBlock

    from server.sessions.events import TextDelta, ThinkingDelta, ToolCall
    options = "Everything shipped. Options:\n1. **Update graphy**\n2. **Verify**"
    closing = "I've handed the session back with the options above."
    t = Translator()
    out: list = []
    out += t.feed(f.stream_start(uuid="m1"))
    out += t.feed(f.stream_thinking("weigh the options", uuid="m1"))
    out += t.feed(f.stream_text(options, uuid="m1"))
    out += t.feed(f.assistant(ThinkingBlock(thinking="weigh the options", signature="sig"),
                              TextBlock(text=options),
                              ToolUseBlock(id="toolu_hb", name=HAND_BACK,
                                           input={"reason": "Pick the next piece"}),
                              uuid="m1"))
    assert t.has_signal("hand_back") and t.message_prose() == options
    out += t.feed(f.user_tool_result("toolu_hb", [{"type": "text", "text": "{}"}]))
    out += t.feed(f.stream_start(uuid="m2"))
    out += t.feed(f.stream_text(closing, uuid="m2"))
    out += t.feed(f.assistant(TextBlock(text=closing), uuid="m2"))
    end = feed_result(t)

    assert [e.text for e in out if isinstance(e, TextDelta)] == [options, closing]
    assert [e.text for e in out if isinstance(e, ThinkingDelta)] == ["weigh the options"]
    assert not any(isinstance(e, ToolCall) for e in out)
    assert end.outcome == "hand_back" and end.hand_back_reason == "Pick the next piece"


# ── A hand_back is surfaced the moment it is READ, not at turn end (#2647) ────────
#
# Prod 2026-10-02 (session bfc8ccbf): `hand_back("Approve a lock-gated dequeue…")`,
# then "While waiting for your approval, I'm starting the gate fix" and 13 more
# minutes of tool calls in the same turn. The signal is latched for the
# ResultMessage, so nothing — no status, no banner, no push — happened until the
# turn ended. The Translator now also emits a HandBackRequested the moment it reads
# the call, so the driver can record the ask while the turn is still running.

def test_a_top_level_hand_back_emits_hand_back_requested_when_read():
    from server.sessions.events import HandBackRequested
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "approve the dequeue"}, pid=None)
    asks = [e for e in out if isinstance(e, HandBackRequested)]
    assert len(asks) == 1
    assert asks[0].reason == "approve the dequeue"
    assert asks[0].turn_id == t._turn_id
    # The turn-end outcome is unchanged: the early event is in ADDITION to the latch.
    assert feed_result(t).outcome == "hand_back"


def test_a_sub_agent_hand_back_emits_no_hand_back_requested():
    """Same guard as the latch: a Task sub-agent's call is its own sub-objective and
    falls through to a card (the AskUserQuestion precedent), never an ask."""
    from server.sessions.events import HandBackRequested, ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "sub ask"}, pid="task_1")
    assert not any(isinstance(e, HandBackRequested) for e in out)
    assert any(isinstance(e, ToolCall) for e in out)
    assert feed_result(t).outcome == "success"


def test_hand_back_requested_is_internal_and_has_no_wire_form():
    """The driver consumes it (pending_hand_back_reason) and re-derives status; the
    browser learns of the ask from the status frame's handBackReason, never from a
    transcript event — there is no card for a top-level hand_back (reducer.ts)."""
    from server.sessions.events import HandBackRequested
    assert not hasattr(HandBackRequested("t1", "r"), "to_wire")


def test_prose_survives_the_sdk_splitting_one_api_message_per_block():
    """The CLI emits [text] and [tool_use] as separate AssistantMessages sharing a
    `message_id`; the hand_back acknowledgement must still see the prose (#3059)."""
    from claude_agent_sdk import ToolUseBlock
    options = "Pick one:\nA) x\nB) y"
    t = Translator()
    t.feed(f.assistant(TextBlock(text=options), uuid="a", message_id="msg_1"))
    t.feed(f.assistant(ToolUseBlock(id="tc_hb", name=HAND_BACK, input={"reason": "pick"}),
                       uuid="b", message_id="msg_1"))
    assert t.has_signal("hand_back") and t.message_prose() == options
    # A NEW API message resets it.
    t.feed(f.assistant(TextBlock(text="after"), uuid="c", message_id="msg_2"))
    assert t.message_prose() == "after"
