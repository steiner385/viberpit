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
