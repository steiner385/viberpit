"""One instruction composition for markers and signalling tools (task-8,
marker-to-tool migration). The property under test: a session is told exactly ONE
way to signal — never both, never neither."""
from __future__ import annotations

from server.sessions.completion import COMPLETION_MARKER, marker_instructions
from server.sessions.tools.instruction import session_instructions
from server.sessions.tools.registry import READ_TOOL_NAMES, SIGNAL_TOOL_NAMES, mounted_names


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
    """It said 'two MCP tools' when there were two. Checked at the READ-only call
    (session_tools=True, signalling_tools=False) rather than the 5-tool call: there,
    the literal count really is 2, so a hardcoded 'two MCP tools' would pass an
    absence check just as well as the generated text does. Asserting the rendered
    count equals `len(mounted_names(...))` is the only version that can actually
    fail if the count is ever hand-typed again."""
    names = mounted_names(signalling_tools=False, self_model_switch=False)
    s = session_instructions(session_tools=True, signalling_tools=False,
                             self_model_switch=False)
    assert f"exposes {len(names)} MCP tools" in s


def test_every_mounted_name_has_a_blurb():
    """`instruction.py:_BLURBS[name]` is a spawn-time KeyError, not a test failure,
    if a name `mounted_names` can produce is missing — i.e. every Claude session
    spawn would crash. Enumerate every combination the flags can actually produce."""
    from server.sessions.tools.instruction import _BLURBS
    for signalling_tools in (False, True):
        for self_model_switch in (False, True):
            names = mounted_names(signalling_tools=signalling_tools,
                                  self_model_switch=self_model_switch)
            assert set(names) <= set(_BLURBS), (signalling_tools, self_model_switch)
    # And the registry's own sources of truth, independent of the flag sweep above.
    assert set(READ_TOOL_NAMES) <= set(_BLURBS)
    assert SIGNAL_TOOL_NAMES <= set(_BLURBS)


def test_session_tools_off_ignores_signalling_tools_regardless_of_its_value():
    """Pins the branch order: `session_tools=False` must short-circuit to the marker
    contract even when `signalling_tools=True` is passed alongside it — safe today
    only because `session_tools` is checked first, and nothing guarded it before this
    test. A future reorder that checked `signalling_tools` first would teach a session
    about tools it does not have declared at all."""
    assert session_instructions(session_tools=False, signalling_tools=True,
                                self_model_switch=False) == marker_instructions(
        self_model_switch=False)
    assert session_instructions(session_tools=False, signalling_tools=True,
                                self_model_switch=True) == marker_instructions(
        self_model_switch=True)


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
