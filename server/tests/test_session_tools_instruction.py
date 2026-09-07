"""One instruction composition for markers and signalling tools (task-8,
marker-to-tool migration). The property under test: a session is told exactly ONE
way to signal — never both, never neither."""
from __future__ import annotations

from server.sessions.completion import COMPLETION_MARKER, marker_instructions
from server.sessions.tools.instruction import session_instructions


def test_a_tools_session_is_never_also_told_about_markers():
    s = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=True)
    assert "mark_complete" in s
    assert "<!--" not in s, "two ways to do one thing is the failure mode this guards"


def test_a_marker_session_is_never_told_about_signalling_tools():
    s = session_instructions(session_tools=True, signalling_tools=False, self_model_switch=False)
    assert COMPLETION_MARKER in s
    assert "mark_complete" not in s


def test_request_model_is_only_named_when_the_engine_can_honour_it():
    on = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=True)
    off = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False)
    assert "request_model" in on and "request_model" not in off


def test_the_surface_off_entirely_is_exactly_the_marker_contract():
    s = session_instructions(session_tools=False, signalling_tools=False, self_model_switch=True)
    assert s == marker_instructions(self_model_switch=True)


def test_the_paragraph_never_hardcodes_a_tool_count():
    """It said 'two MCP tools' when there were two. Generated from the mounted set
    so it cannot drift from what was declared."""
    assert "two MCP tools" not in session_instructions(
        session_tools=True, signalling_tools=True, self_model_switch=True)


def test_no_marker_session_still_gets_the_read_tools_paragraph():
    """session_tools on, signalling off (today's Codex shape, and Claude with the
    surface on but no durable_tool_surface): the two READ tools are still named,
    alongside the full marker contract — not one or the other."""
    s = session_instructions(session_tools=True, signalling_tools=False, self_model_switch=True)
    assert "session_context" in s and "report_feedback" in s
    assert marker_instructions(self_model_switch=True) in s
    assert "mark_complete" not in s and "mark_no_pr" not in s and "request_model" not in s


def test_mark_no_pr_is_also_named_when_signalling_is_on():
    s = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False)
    assert "mark_no_pr" in s
