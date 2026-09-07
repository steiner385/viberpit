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
    feed_assistant_tool_call(t, "mcp__ccswitch__mark_complete", {}, pid=None)
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


def test_a_signalling_tool_call_is_not_rendered_as_a_tool_card():
    """A ToolCall card for `mark_complete` is noise on top of the status change it
    causes — the same reason AskUserQuestion is skipped in the loop above it."""
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    assert not any(isinstance(e, ToolCall) for e in out)


def test_a_non_signalling_ccswitch_tool_still_renders_a_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, "mcp__viberpit__session_context", {}, pid=None)
    assert any(isinstance(e, ToolCall) for e in out)


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
