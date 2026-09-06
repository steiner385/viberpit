"""Deterministic-grader tests for the AI-assistant eval runner (plan Task 4.2).

Only the network-free half of ``scripts/eval_assistant.py`` is unit tested here: case
loading and ``grade_tool_sequence``. The live half (a real assistant turn + a Haiku
judge) needs a configured Anthropic account and is exercised by actually running the
script, not by pytest — see ``docs/superpowers/evals/assistant/results-*.md``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_assistant.py"
_SPEC = importlib.util.spec_from_file_location("eval_assistant", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
eval_assistant = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = eval_assistant
_SPEC.loader.exec_module(eval_assistant)

EvalCase = eval_assistant.EvalCase
load_cases = eval_assistant.load_cases
grade_tool_sequence = eval_assistant.grade_tool_sequence


def _case(**kw):
    defaults = dict(id="c1", prompt="p", route="/sessions",
                    expect_tools=(), forbid_tools=(), rubric="")
    defaults.update(kw)
    return EvalCase(**defaults)


def test_passes_when_every_expected_tool_was_called():
    case = _case(expect_tools=("list_sessions",))
    grade = grade_tool_sequence(case, ["list_sessions"])
    assert grade.passed is True
    assert grade.missing == ()
    assert grade.forbidden_hit == ()


def test_fails_when_an_expected_tool_never_ran():
    case = _case(expect_tools=("list_sessions", "get_session"))
    grade = grade_tool_sequence(case, ["list_sessions"])
    assert grade.passed is False
    assert grade.missing == ("get_session",)


def test_fails_when_a_forbidden_tool_ran():
    case = _case(expect_tools=("list_sessions",), forbid_tools=("delete_session",))
    grade = grade_tool_sequence(case, ["list_sessions", "delete_session"])
    assert grade.passed is False
    assert grade.forbidden_hit == ("delete_session",)
    # The expected tool still ran — only the forbidden hit sinks the case.
    assert grade.missing == ()


def test_passes_with_no_expectations_and_nothing_forbidden():
    case = _case()
    grade = grade_tool_sequence(case, [])
    assert grade.passed is True


def test_order_and_repeat_calls_do_not_matter():
    case = _case(expect_tools=("get_session", "resolve_permission"))
    grade = grade_tool_sequence(
        case, ["get_session", "get_session", "resolve_permission", "get_session"])
    assert grade.passed is True


def test_case_id_is_carried_onto_the_grade():
    case = _case()
    assert grade_tool_sequence(case, []).case_id == "c1"


def test_load_cases_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "dupe.jsonl"
    path.write_text(
        '{"id": "a", "prompt": "x", "route": "/sessions"}\n'
        '{"id": "a", "prompt": "y", "route": "/sessions"}\n'
    )
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(path)


def test_load_cases_skips_blank_lines_and_defaults_missing_fields(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('\n{"id": "a", "prompt": "x", "route": "/sessions"}\n\n')
    cases = load_cases(path)
    assert len(cases) == 1
    assert cases[0].expect_tools == ()
    assert cases[0].forbid_tools == ()
    assert cases[0].rubric == ""


# ---------------------------------------------------------------------------
# Sanity checks on the shipped case file — still network-free and deterministic.
# ---------------------------------------------------------------------------

_SHIPPED_CASES = load_cases()


def test_shipped_case_file_has_around_fifty_cases():
    # Grew from the original ~40: Task 5.1 added 15 starter-chip cases, Task 8.1 added 8
    # notification-tool cases, Tasks 5.3/5.4 added 5 for repo-discovery and transcript depth.
    #
    # The ceiling is deliberately far above today's total rather than just clear of it. This
    # is a smoke check that the file was not truncated or duplicated, not a budget — and the
    # conformance rule "one eval case per tool" means every new tool RAISES the count, so a
    # ceiling pinned near the current size turns each new tool into an unrelated failure
    # here. Two Phase-5/8 branches hit the old ceiling within an hour of each other; Phase 6
    # alone adds 22 more tools.
    assert 45 <= len(_SHIPPED_CASES) <= 120


def test_shipped_case_file_has_at_least_six_injection_cases():
    injection = [c for c in _SHIPPED_CASES if "delete_session" in c.forbid_tools]
    assert len(injection) >= 6


def test_shipped_case_ids_are_unique():
    ids = [c.id for c in _SHIPPED_CASES]
    assert len(ids) == len(set(ids))


def test_shipped_cases_reference_only_real_tool_names():
    from server.assistant.tools import TOOL_SPECS

    for case in _SHIPPED_CASES:
        for name in (*case.expect_tools, *case.forbid_tools):
            assert name in TOOL_SPECS, f"{case.id}: unknown tool {name!r}"
