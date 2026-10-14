"""Parked review, pure half (design 2026-10-14 §4): when a run is due, which parked
issues it covers, how a reply is read, and what becomes of a verdict — only ever an
owner recommendation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.pm import hygiene as h
from server.pm import parked_review as pr
from server.pm.outcomes import body_fingerprint
from server.pm.queue import QueueRow

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _row(number: int, **over: Any) -> QueueRow:
    base: dict[str, Any] = dict(
        ref=f"o/r#{number}", repo="o/r", number=number, product="p", title=f"Issue {number}",
        body="body", url="u", status="Backlog", score=None,
        gh_created_at=NOW - timedelta(days=100 - number), gh_updated_at=NOW, assignees=(),
        author="tony", labels=("Parked",), open_children=0, open_blockers=(),
        live_session=False, claimed=False, work_item_id=f"wi{number}")
    base.update(over)
    return QueueRow(**base)


def _state(row: QueueRow, verdict: str | None, **over: Any) -> dict:
    v = pr.Verdict(verdict, "x.py was removed in #5", "s", 5 if verdict == "close_fixed"
                   else None) if verdict else None
    st = pr.review_state(fingerprint=body_fingerprint(row.title, row.body), verdict=v,
                         prs=[{"number": 5, "title": "feat"}], repo=row.repo, at=NOW.isoformat())
    st.update(over)
    return st


@pytest.mark.parametrize("last,parked,threshold,due", [
    (None, 0, 20, True),                                  # never run
    (NOW - timedelta(days=7), 3, 20, True),               # weekly
    (NOW - timedelta(days=6), 3, 20, False),
    (NOW - timedelta(days=1), 21, 20, True),              # over the threshold: daily
    (NOW - timedelta(hours=23), 21, 20, False),
    (NOW - timedelta(days=1), 20, 20, False),             # AT the threshold is not over
    (NOW - timedelta(days=2), 99, 0, False),              # 0 = weekly only
])
def test_run_due_weekly_or_daily_over_the_threshold(last, parked, threshold, due):
    assert pr.run_due(last, parked, threshold, now=NOW) is due


def test_pick_takes_never_reviewed_first_then_the_least_recently_reviewed():
    rows = [_row(1), _row(2), _row(3), _row(4)]
    states = {"o/r#1": {"at": (NOW - timedelta(days=1)).isoformat()},
              "o/r#2": {"at": (NOW - timedelta(days=9)).isoformat()}}
    assert [r.ref for r in pr.pick(rows, states, 3)] == ["o/r#3", "o/r#4", "o/r#2"]


def test_parked_rows_are_the_labelled_ones_any_case():
    rows = [_row(1), _row(2, labels=("bug",)), _row(3, labels=("PARKED", "x"))]
    assert [r.ref for r in pr.parked_rows(rows)] == ["o/r#1", "o/r#3"]


def test_prompt_lists_the_issue_and_each_merged_pr():
    text = pr.build_prompt("Old thing", "please do x", [
        {"number": 5, "title": "feat: x", "body": "Fixes nothing", "files": ["x.py"],
         "diff_excerpt": "+x"}])
    assert "Parked issue: Old thing" in text and "Merged PR #5: feat: x" in text
    assert "x.py" in text and "```diff" in text
    assert "No merged pull request" in pr.build_prompt("t", "b", [])


@pytest.mark.parametrize("reply,merged,expected", [
    ('{"verdict": "close_fixed", "pr": 5, "evidence": "#5 adds x"}', [5], ("close_fixed", 5)),
    ('Sure: {"verdict": "unpark", "evidence": "", "summary": "go"}', [], ("unpark", None)),
    ('{"verdict": "keep"}', [], ("keep", None)),
    ('{"verdict": "close_obsolete", "evidence": "x.py is gone"}', [], ("close_obsolete", None)),
])
def test_parse_verdict_reads_a_usable_reply(reply, merged, expected):
    v = pr.parse_verdict(reply, merged)
    assert v is not None and (v.verdict, v.pr) == expected


@pytest.mark.parametrize("reply,merged", [
    ("I think it is fine", []),
    ('{"verdict": "delete"}', []),
    # close_fixed must cite a merged PR the prompt listed, with evidence
    ('{"verdict": "close_fixed", "pr": 9, "evidence": "#9"}', [5]),
    ('{"verdict": "close_fixed", "pr": null, "evidence": "it is done"}', [5]),
    ('{"verdict": "close_fixed", "pr": 5, "evidence": ""}', [5]),
    ('{"verdict": "close_obsolete", "evidence": ""}', []),
])
def test_parse_verdict_refuses_an_unusable_reply(reply, merged):
    assert pr.parse_verdict(reply, merged) is None


def test_review_state_carries_evidence_links_and_no_applied_stamp():
    st = _state(_row(1), "close_fixed")
    assert st["prs"] == [{"number": 5, "title": "feat", "url": "https://github.com/o/r/pull/5"}]
    assert (st["verdict"], st["pr"], st["appliedAt"]) == ("close_fixed", 5, None)


# --- hygiene rule 6 --------------------------------------------------------------------------

def test_rule_turns_fixed_obsolete_and_unpark_into_recommendations_and_keeps_keep():
    rows = [_row(1), _row(2), _row(3, labels=("Parked",)), _row(4)]
    states = {"o/r#1": _state(rows[0], "close_fixed"),
              "o/r#2": _state(rows[1], "close_obsolete"),
              "o/r#3": _state(rows[2], "unpark"),
              "o/r#4": _state(rows[3], "keep")}
    out = {a.ref: a for a in h.parked_review(rows, states)}
    assert set(out) == {"o/r#1", "o/r#2", "o/r#3"}
    fixed, obsolete, unpark = out["o/r#1"], out["o/r#2"], out["o/r#3"]
    assert (fixed.action, fixed.detail["closeReason"]) == ("close", "completed")
    assert "https://github.com/o/r/pull/5" in fixed.detail["comment"]
    assert fixed.detail["prs"][0]["url"].endswith("/pull/5")
    assert (obsolete.action, obsolete.detail["closeReason"]) == ("close", "not_planned")
    assert (unpark.action, unpark.detail["label"]) == ("unpark", "Parked")
    assert all(a.rule == h.RULE_PARKED_REVIEW for a in out.values())
    assert h.RULE_PARKED_REVIEW in h.RECOMMEND_ONLY_RULES


def test_rule_skips_a_changed_body_an_applied_review_and_an_unparked_issue():
    row = _row(1)
    assert h.parked_review([row], {"o/r#1": _state(row, "unpark", fingerprint="old")}) == []
    assert h.parked_review([row], {"o/r#1": _state(row, "unpark", appliedAt="2026")}) == []
    unparked = _row(1, labels=("bug",))
    assert h.parked_review([unparked], {"o/r#1": _state(row, "unpark")}) == []
    assert h.parked_review([row], {"o/r#1": _state(row, None)}) == []


def test_plan_includes_parked_review_findings():
    row = _row(1)
    inputs = h.ProductInputs(slug="p", project_id="pid", policy={}, rows=[row],
                             board_updated_at={}, stale_edges=[],
                             parked_states={"o/r#1": _state(row, "unpark")})
    assert [(a.rule, a.action) for a in h.plan(inputs, now=NOW)] == [("parked_review", "unpark")]
