"""Tests for session PR proposals and review fetching."""
import subprocess

from server.sessions import dispatch


def test_broken_pr_proposal_text():
    assert dispatch.broken_pr_proposal_text(218, "fix the thing") == "Fix the failing CI on PR #218: fix the thing"


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


def test_fetch_unresolved_review_count_exception_returns_zero():
    """A raising gh runner (e.g. gh missing) → returns 0, never propagates."""
    def boom(args):
        raise OSError("gh not found")

    assert dispatch.fetch_unresolved_review_count("o/r", 7, run_gh=boom) == 0
