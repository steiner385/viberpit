"""Tests for server.sessions.dispatch (Task 1 — pure pick + gh fetch)."""
import subprocess

from server.sessions import dispatch


def test_proposal_text():
    assert dispatch.issue_proposal_text({"number": 12, "title": "Fix the thing"}) == "Work on issue #12: Fix the thing"


def test_pick_top_prefers_priority_label():
    issues = [
        {"number": 1, "title": "a", "labels": ["bug"], "updatedAt": "2026-06-24T10:00:00Z"},
        {"number": 2, "title": "b", "labels": ["priority"], "updatedAt": "2026-06-20T10:00:00Z"},
    ]
    assert dispatch.pick_top_issue(issues)["number"] == 2


def test_pick_top_falls_back_to_recency():
    issues = [
        {"number": 1, "title": "a", "labels": [], "updatedAt": "2026-06-20T10:00:00Z"},
        {"number": 2, "title": "b", "labels": [], "updatedAt": "2026-06-24T10:00:00Z"},
    ]
    assert dispatch.pick_top_issue(issues)["number"] == 2


def test_pick_top_empty_is_none():
    assert dispatch.pick_top_issue([]) is None


def test_broken_pr_proposal_text():
    assert dispatch.broken_pr_proposal_text(218, "fix the thing") == "Fix the failing CI on PR #218: fix the thing"


def test_fetch_open_issues_parses(monkeypatch):
    def fake_gh(args):
        assert "issue" in args and "list" in args
        return subprocess.CompletedProcess(args, 0, stdout='[{"number":3,"title":"t","labels":[{"name":"Bug"}],"updatedAt":"x"}]', stderr="")
    out = dispatch.fetch_open_issues("o/r", run_gh=fake_gh)
    assert out[0]["number"] == 3 and out[0]["labels"] == ["bug"]


def test_fetch_open_issues_error_is_empty(monkeypatch):
    def boom(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh fail")
    assert dispatch.fetch_open_issues("o/r", run_gh=boom) == []


# ---------------------------------------------------------------------------
# review_proposal_text
# ---------------------------------------------------------------------------

def test_review_proposal_text_plural():
    assert dispatch.review_proposal_text(3, "t", 3) == "Address 3 review comments on PR #3: t"


def test_review_proposal_text_singular():
    assert dispatch.review_proposal_text(7, "my title", 1) == "Address 1 review comment on PR #7: my title"


# ---------------------------------------------------------------------------
# fetch_unresolved_review_count
# ---------------------------------------------------------------------------

def _graphql_response(nodes):
    import json
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
    return json.dumps(payload)


def test_fetch_unresolved_review_count_returns_count():
    """2 unresolved + 1 resolved → returns 2."""
    nodes = [
        {"isResolved": False},
        {"isResolved": True},
        {"isResolved": False},
    ]

    def fake_gh(args):
        return subprocess.CompletedProcess(args, 0, stdout=_graphql_response(nodes), stderr="")

    assert dispatch.fetch_unresolved_review_count("o/r", 7, run_gh=fake_gh) == 2


def test_fetch_unresolved_review_count_nonzero_rc_returns_zero():
    """Non-zero exit code → returns 0 without raising."""
    def fake_gh(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="error")

    assert dispatch.fetch_unresolved_review_count("o/r", 7, run_gh=fake_gh) == 0
