"""Close-out assessment, pure half (design 2026-10-14 §3): which holds qualify, the
prompt's bounds, a tolerant parse, and the decision matrix."""
from __future__ import annotations

import json

import pytest

from server.automations.backlog_github import (CLOSEOUT_HOLD_PREFIX,
                                               LEGACY_CLOSEOUT_HOLD_REASON,
                                               closeout_hold_reason)
from server.pm import closeout_assess as ca


def test_closeout_hold_reason_is_built_from_the_exported_prefix():
    assert closeout_hold_reason("x").startswith(CLOSEOUT_HOLD_PREFIX)
    assert closeout_hold_reason("") == LEGACY_CLOSEOUT_HOLD_REASON


@pytest.mark.parametrize("kind,reason,expected", [
    ("owner_action", closeout_hold_reason("the PR references it with `Refs`"), True),
    ("owner_action", LEGACY_CLOSEOUT_HOLD_REASON, True),
    ("owner_action", "bounced at opus; fable needs approval", False),
    ("owner_action", "Left for you:\n- [ ] check prod", False),
    ("decision", closeout_hold_reason("x"), False),
    ("owner_action", None, False),
])
def test_is_closeout_hold(kind, reason, expected):
    assert ca.is_closeout_hold(kind, reason) is expected


def test_prompt_lists_unchecked_items_else_the_rest_and_bounds_the_diff():
    prs = [{"number": 5, "title": "feat", "body": "Refs #9", "files": ["a.py", "b.py"],
            "diff_excerpt": "+x\n" * 5000},
           {"number": 6, "title": "more", "body": "", "files": ["c.py"],
            "diff_excerpt": "+y\n" * 5000}]
    p = ca.build_prompt("Title", "body", ["QA retest on main", "Add docs"], prs)
    assert "1. QA retest on main" in p and "2. Add docs" in p
    assert "Merged PR #5: feat" in p and "a.py, b.py" in p
    assert p.count("+x") + p.count("+y") <= ca.DIFF_EXCERPT_MAX // 3 + 1
    assert len(p) < ca.DIFF_EXCERPT_MAX + 6_000
    rest = ca.build_prompt("T", "b", [], [])
    assert ca.REST_OF_ISSUE in rest and "No merged PR could be read" in rest


def _reply(*items, summary="s"):
    return json.dumps({"items": [{"text": t, "verdict": v, "evidence": e}
                                 for t, v, e in items], "summary": summary})


def test_parse_is_tolerant_of_prose_and_fences_and_refuses_garbage():
    body = _reply(("a", "done", "x.py:3"))
    got = ca.parse_verdict(f"Here you go:\n```json\n{body}\n```")
    assert got == ca.Assessment((ca.AssessedItem("a", "done", "x.py:3"),), "s")
    assert ca.parse_verdict("no json at all") is None
    assert ca.parse_verdict(None) is None
    assert ca.parse_verdict('{"items": []}') is None
    assert ca.parse_verdict(_reply(("a", "maybe", ""))) is None
    assert ca.parse_verdict('{"items": [{"verdict": "done"}]}') is None


def test_all_done_closes_citing_the_evidence():
    d = ca.decide(ca.parse_verdict(_reply(("a", "done", "x.py:3"), ("b", "done", "y.py:9"))))
    assert d is not None and d.kind == ca.DECISION_CLOSE
    assert "a — x.py:3" in d.text and "b — y.py:9" in d.text


def test_agent_doable_without_human_releases():
    d = ca.decide(ca.parse_verdict(_reply(("a", "done", "x"), ("add a test", "agent_doable", ""))))
    assert d is not None and d.kind == ca.DECISION_RELEASE and "add a test" in d.text


def test_any_human_only_item_becomes_the_checklist_of_only_those():
    d = ca.decide(ca.parse_verdict(_reply(("a", "done", "x"), ("b", "agent_doable", ""),
                                          ("verify on prod", "human_only", ""))))
    assert d is not None and d.kind == ca.DECISION_CHECKLIST
    assert d.text == "Left for you:\n- [ ] verify on prod"
    assert ca.is_checklist_reason(d.text)


def test_checklist_shows_five_then_counts_and_stays_short():
    text = ca.checklist_reason([f"item {i} " + "x" * 200 for i in range(8)])
    assert len(text) <= ca.CHECKLIST_MAX_CHARS
    assert text.startswith(ca.CHECKLIST_PREFIX) and "more)" in text
    assert text.count("- [ ]") <= ca.CHECKLIST_SHOWN
    short = ca.checklist_reason([f"i{i}" for i in range(7)])
    assert short.count("- [ ]") == 5 and short.endswith("(+2 more)")


def test_garbage_decides_nothing():
    assert ca.decide(None) is None
