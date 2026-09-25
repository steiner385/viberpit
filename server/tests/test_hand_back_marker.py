"""The hand-back marker (Codex + permanent fallback) and the shared tool-argument
parser — spec docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md §3-4."""
from __future__ import annotations

import pytest

from server.mcp_spec import ToolArgError
from server.sessions.completion import (
    COMPLETION_INSTRUCTION, COMPLETION_MARKER, HAND_BACK_INSTRUCTION, HAND_BACK_MARKER,
    HAND_BACK_MARKER_RE, HAND_BACK_REASON_MAX, NO_PR_INSTRUCTION, marker_instructions,
    parse_hand_back, parse_hand_back_marker, strip_markers,
)


def test_marker_is_an_html_comment_in_the_current_brand():
    assert HAND_BACK_MARKER == "<!-- viberpit:hand-back -->"


@pytest.mark.parametrize("text,reason", [
    ('<!-- viberpit:hand-back reason="need a decision on scope" -->', "need a decision on scope"),
    ("<!-- viberpit:hand-back -->", None),
    ("<!--viberpit:hand-back-->", None),
    ('<!-- ccswitch:hand-back reason="pre-rebrand transcript" -->', "pre-rebrand transcript"),
])
def test_marker_parses_either_slug_with_or_without_a_reason(text, reason):
    assert parse_hand_back_marker(f"Which one?\n{text}") == {"reason": reason}


def test_marker_absent_returns_none():
    assert parse_hand_back_marker("all done") is None
    assert parse_hand_back_marker("") is None
    assert parse_hand_back_marker(None) is None


def test_marker_last_wins_and_empty_reason_reads_as_none():
    text = ('<!-- viberpit:hand-back reason="first" -->\n'
            '<!-- viberpit:hand-back reason="" -->')
    assert parse_hand_back_marker(text) == {"reason": None}


def test_regex_never_swallows_the_rest_of_the_message():
    m = HAND_BACK_MARKER_RE.search('<!-- viberpit:hand-back reason="a" --> trailing text')
    assert m is not None and m.group(0).endswith("-->")


def test_strip_markers_removes_the_hand_back_marker_so_prose_heuristics_see_the_tail():
    text = 'Wire it or remove it?\n<!-- viberpit:hand-back reason="wire vs remove" -->'
    assert strip_markers(text) == "Wire it or remove it?"


def test_parse_hand_back_trims_and_accepts():
    assert parse_hand_back({"reason": "  need a decision  "}) == {"reason": "need a decision"}


def test_parse_hand_back_truncates_a_long_reason_rather_than_rejecting_it():
    long = "x" * (HAND_BACK_REASON_MAX + 50)
    assert parse_hand_back({"reason": long}) == {"reason": "x" * HAND_BACK_REASON_MAX}


@pytest.mark.parametrize("args", [{}, {"reason": ""}, {"reason": "   "}, {"reason": 7}, None])
def test_parse_hand_back_rejects_a_missing_or_blank_reason(args):
    with pytest.raises(ToolArgError) as ei:
        parse_hand_back(args)
    assert "reason" in str(ei.value)


def test_instruction_teaches_the_marker_and_the_three_way_framing():
    low = HAND_BACK_INSTRUCTION.lower()
    assert "viberpit:hand-back" in HAND_BACK_INSTRUCTION
    assert 'reason="' in HAND_BACK_INSTRUCTION
    assert COMPLETION_MARKER in HAND_BACK_INSTRUCTION
    assert "one of three ways" in low
    assert "not a hand-back" in low


def test_marker_instructions_include_the_hand_back_instruction():
    assert HAND_BACK_INSTRUCTION in marker_instructions(self_model_switch=False)
    assert HAND_BACK_INSTRUCTION in marker_instructions(self_model_switch=True)


def test_the_finishing_instructions_point_at_the_hand_back_marker():
    for text in (COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION):
        assert "viberpit:hand-back" in text


def test_the_marker_instructions_carry_the_finished_state_and_no_status_only_clauses():
    """Prod 2026-09-25 (session 2971d6a6): PR merged, follow-up PR ready with auto-merge
    armed and CI still running, and the session ended with a hand-back whose reason began
    "Status only, no action needed". The completion bar already reads an armed PR as met,
    but the instruction said "never while … waiting on something", so a session waiting
    on CI had no finishing signal it was allowed to use and fell through to the only one
    left. Both clauses are shared constants so the marker world and the tool
    descriptions (test_session_tools.py) cannot drift."""
    from server.sessions.completion import FINISHED_STATE_CLAUSE, STATUS_ONLY_CLAUSE
    assert FINISHED_STATE_CLAUSE in COMPLETION_INSTRUCTION
    assert STATUS_ONLY_CLAUSE in HAND_BACK_INSTRUCTION
    assert "waiting on something" not in COMPLETION_INSTRUCTION
    low = FINISHED_STATE_CLAUSE.lower()
    assert "auto-merge" in low and "finished" in low
    assert "must ask" in STATUS_ONLY_CLAUSE.lower()
