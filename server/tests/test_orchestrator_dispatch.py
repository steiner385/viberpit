"""Tests for M3a orchestrator dispatch-proposal integration (Task 2).

Tests:
- _dispatch_proposal returns None for a repo-less record.
- After awaiting _refresh_issue_proposal("o/r"), _dispatch_proposal returns the proposal.
- TTL: a second _dispatch_proposal within TTL does not re-schedule a fetch.
- latest_suggestions for a detached done session with a repo includes the proposal as FIRST item.
"""
from __future__ import annotations

import asyncio

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch():
    """Minimal orchestrator with only the dispatch-related state."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._issue_proposals = {}
    orch._issue_inflight = set()
    orch._issue_numbers = {}
    orch._proposed_issue = {}
    orch._review_proposals = {}
    orch._review_inflight = set()
    orch._detached_suggestions = {}
    orch._suggest_inflight = set()
    orch._suggester = None
    orch._clock = None  # Use default time.monotonic
    return orch


def _make_record(session_id: str, repo: str | None = None, status: str = "done") -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name="test",
        account="acct-a",
        claude_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        status=status,
    )


# ---------------------------------------------------------------------------
# Test: _dispatch_proposal returns None for a repo-less record
# ---------------------------------------------------------------------------

def test_dispatch_proposal_none_for_no_repo():
    orch = _make_orch()
    rec = _make_record("s1", repo=None)
    result = orch._dispatch_proposal(rec)
    assert result is None


def _record_with_branch(session_id: str, repo: str, branch: str, status: str = "done") -> SessionRecord:
    return SessionRecord(
        id=session_id, name="test", account="acct-a", claude_session_id="",
        cols=80, rows=24, cwd="/tmp", repo=repo, branch=branch, status=status,
    )


def test_dispatch_proposal_prefers_broken_pr_over_issue():
    """M3b: a session whose own PR is failing CI gets the fix proposal, not an issue."""
    from server.github.pr_status import PullRequestInfo
    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    orch.pr_for = lambda repo, branch: PullRequestInfo(218, "u", "open", "failing", "fix the thing")
    # Even with a cached issue proposal, the broken PR wins.
    import time
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #5: x")
    assert orch._dispatch_proposal(rec) == "Fix the failing CI on PR #218: fix the thing"


def test_dispatch_proposal_non_failing_pr_falls_through_to_issue():
    """M3b/M3c: a passing PR with no reviews falls through to the issue source.

    Seed the review cache with None so the cascade falls through without
    scheduling a background asyncio.create_task (no event loop in sync tests).
    """
    from server.github.pr_status import PullRequestInfo
    import time
    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    orch.pr_for = lambda repo, branch: PullRequestInfo(9, "u", "open", "passing", "t")
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #5: x")
    # Seed review cache with None so no background task is scheduled
    orch._review_proposals[("o/r", 9)] = (time.monotonic(), None)
    assert orch._dispatch_proposal(rec) == "Work on issue #5: x"


# ---------------------------------------------------------------------------
# Test: After refreshing, _dispatch_proposal returns the proposal string
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_proposal_returns_after_refresh(monkeypatch):
    def fake_fetch(repo):
        return [{"number": 42, "title": "Important fix", "labels": [], "updatedAt": "2026-06-24T10:00:00Z"}]

    monkeypatch.setattr(orch_mod.dispatch, "fetch_open_issues", fake_fetch)

    orch = _make_orch()
    rec = _make_record("s1", repo="o/r")

    # Before refresh: cache miss → returns None, schedules background task
    result_before = orch._dispatch_proposal(rec)
    assert result_before is None
    assert "o/r" in orch._issue_inflight

    # Run the refresh directly (simulating the background task)
    await orch._refresh_issue_proposal("o/r")

    # Cache is now populated
    assert "o/r" in orch._issue_proposals
    assert "o/r" not in orch._issue_inflight

    result_after = orch._dispatch_proposal(rec)
    assert result_after == "Work on issue #42: Important fix"


# ---------------------------------------------------------------------------
# Test: Second _dispatch_proposal within TTL does NOT re-schedule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_proposal_ttl_no_reschedule(monkeypatch):
    fetch_calls: list[str] = []

    def fake_fetch(repo):
        fetch_calls.append(repo)
        return [{"number": 7, "title": "A task", "labels": [], "updatedAt": "2026-06-24T10:00:00Z"}]

    monkeypatch.setattr(orch_mod.dispatch, "fetch_open_issues", fake_fetch)

    orch = _make_orch()
    rec = _make_record("s1", repo="o/r")

    # Seed the cache with a fresh timestamp
    import time
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #7: A task")

    # Should return the cached proposal without scheduling a new fetch
    tasks_before = len(asyncio.all_tasks())
    result = orch._dispatch_proposal(rec)
    tasks_after = len(asyncio.all_tasks())

    assert result == "Work on issue #7: A task"
    assert len(fetch_calls) == 0
    assert tasks_after == tasks_before  # no new task scheduled


# ---------------------------------------------------------------------------
# Test: latest_suggestions prepends proposal as FIRST item for done detached session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_latest_suggestions_prepends_proposal_for_done_session(monkeypatch):
    """For a detached done session with repo + cached suggestions, proposal is first."""
    def fake_fetch(repo):
        return [{"number": 99, "title": "Top issue", "labels": ["priority"], "updatedAt": "2026-06-24T00:00:00Z"}]

    monkeypatch.setattr(orch_mod.dispatch, "fetch_open_issues", fake_fetch)

    orch = _make_orch()
    rec = _make_record("s1", repo="o/r", status="done")
    orch._records["s1"] = rec

    # Stub the transcript path resolution so it returns a path without real file I/O
    async def fake_resolve_transcript(session_id):
        return "/fake/transcript.jsonl"

    monkeypatch.setattr(orch, "_resolve_transcript_path", fake_resolve_transcript)

    # Stub os.path.getmtime to return a stable signature
    import os
    monkeypatch.setattr(os.path, "getmtime", lambda p: 12345.0)

    # Seed the detached suggestions cache with known items
    orch._detached_suggestions["s1"] = (12345.0, ["Run tests", "Commit"])

    # Seed the issue proposals cache so _dispatch_proposal returns immediately
    import time
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #99: Top issue")

    result = await orch.latest_suggestions("s1")
    assert result[0] == "Work on issue #99: Top issue"
    assert "Run tests" in result
    assert result.count("Work on issue #99: Top issue") == 1  # not duplicated


# ---------------------------------------------------------------------------
# M3c: review-comment proposal (Task 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_review_proposal_stores_text(monkeypatch):
    """After _refresh_review_proposal, _review_proposal returns the formatted string."""
    monkeypatch.setattr(orch_mod.dispatch, "fetch_unresolved_review_count", lambda repo, number: 2)

    orch = _make_orch()
    await orch._refresh_review_proposal("o/r", 7, "t")

    result = orch._review_proposal("o/r", 7, "t")
    assert result == "Address 2 review comments on PR #7: t"


@pytest.mark.asyncio
async def test_dispatch_proposal_review_between_broken_pr_and_issue(monkeypatch):
    """Cascade: passing PR with unresolved reviews returns review proposal, NOT issue."""
    from server.github.pr_status import PullRequestInfo
    import time

    monkeypatch.setattr(orch_mod.dispatch, "fetch_unresolved_review_count", lambda repo, number: 3)

    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    # Passing PR
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "passing", "PR title")
    # Seed a fresh issue proposal — review should win
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #5: x")
    # Seed a fresh review cache with unresolved count
    orch._review_proposals[("o/r", 7)] = (time.monotonic(), "Address 3 review comments on PR #7: PR title")

    result = orch._dispatch_proposal(rec)
    assert result == "Address 3 review comments on PR #7: PR title"


def test_dispatch_proposal_failing_pr_wins_over_review():
    """Cascade priority: a failing PR proposal is returned, review not consulted."""
    from server.github.pr_status import PullRequestInfo
    import time

    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    # Failing PR
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "failing", "PR title")
    # Seed review cache with unresolved reviews
    orch._review_proposals[("o/r", 7)] = (time.monotonic(), "Address 2 review comments on PR #7: PR title")

    result = orch._dispatch_proposal(rec)
    assert result == "Fix the failing CI on PR #7: PR title"


def test_dispatch_proposal_passing_pr_no_reviews_falls_through_to_issue():
    """Passing PR with zero unresolved reviews (cache → None) falls through to issue."""
    from server.github.pr_status import PullRequestInfo
    import time

    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    # Passing PR
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "passing", "PR title")
    # Review cache seeded with None (no unresolved reviews)
    orch._review_proposals[("o/r", 7)] = (time.monotonic(), None)
    # Issue cache seeded
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #5: x")

    result = orch._dispatch_proposal(rec)
    assert result == "Work on issue #5: x"


def test_dispatch_proposal_collision_avoidance_across_sessions():
    """US-8: two idle sessions in the same repo are not both pointed at the same top issue."""
    import time
    orch = _make_orch()
    orch._issue_numbers["o/r"] = 42
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #42: x")
    r1 = _make_record("s1", repo="o/r")
    r2 = _make_record("s2", repo="o/r")
    orch._records["s1"] = r1
    orch._records["s2"] = r2

    # First session gets the issue and claims it.
    assert orch._dispatch_proposal(r1) == "Work on issue #42: x"
    # Second session in the same repo is NOT pointed at the same issue.
    assert orch._dispatch_proposal(r2) is None
    # The first session still gets it on re-call (its own claim never blocks itself).
    assert orch._dispatch_proposal(r1) == "Work on issue #42: x"


def test_dispatch_proposal_claim_released_when_session_no_longer_idle():
    """A held issue frees up once its session leaves idle/done."""
    import time
    orch = _make_orch()
    orch._issue_numbers["o/r"] = 42
    orch._issue_proposals["o/r"] = (time.monotonic(), "Work on issue #42: x")
    r1 = _make_record("s1", repo="o/r", status="done")
    r2 = _make_record("s2", repo="o/r", status="done")
    orch._records["s1"] = r1
    orch._records["s2"] = r2
    assert orch._dispatch_proposal(r1) == "Work on issue #42: x"   # s1 claims #42
    assert orch._dispatch_proposal(r2) is None                     # blocked

    # s1 starts working → its claim is released on the next dispatch sweep.
    orch._records["s1"] = _make_record("s1", repo="o/r", status="in_progress")
    assert orch._dispatch_proposal(r2) == "Work on issue #42: x"   # now free for s2
