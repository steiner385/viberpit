"""Session suggestions stay grounded in the conversation and its own PR."""
from __future__ import annotations

import hashlib
import time
from unittest.mock import AsyncMock, Mock

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.sessions import events as ev


def _make_orch():
    """Minimal orchestrator with only the dispatch-related state."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
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
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        status=status,
    )


def test_dispatch_proposal_none_for_no_repo():
    orch = _make_orch()
    rec = _make_record("s1", repo=None)
    result = orch._dispatch_proposal(rec)
    assert result is None


def _record_with_branch(session_id: str, repo: str, branch: str, status: str = "done") -> SessionRecord:
    return SessionRecord(
        id=session_id, name="test", account="acct-a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
        cols=80, rows=24, cwd="/tmp", repo=repo, branch=branch, status=status,
    )


def test_dispatch_proposal_suggests_fixing_own_broken_pr():
    """A session whose own PR is failing CI gets the fix proposal."""
    from server.github.pr_status import PullRequestInfo
    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    orch.pr_for = lambda repo, branch: PullRequestInfo(218, "u", "open", "failing", "fix the thing")
    assert orch._dispatch_proposal(rec) == "Fix the failing CI on PR #218: fix the thing"


def test_dispatch_proposal_passing_pr_without_reviews_returns_none():
    """A passing PR with no reviews has no deterministic next action.

    Seed the review cache with None so the cascade falls through without
    scheduling a background asyncio.create_task (no event loop in sync tests).
    """
    from server.github.pr_status import PullRequestInfo
    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    orch.pr_for = lambda repo, branch: PullRequestInfo(9, "u", "open", "passing", "t")
    # Seed review cache with None so no background task is scheduled
    orch._review_proposals[("o/r", 9)] = (time.monotonic(), None)
    assert orch._dispatch_proposal(rec) is None


def test_dispatch_proposal_ignores_a_red_no_check_earned():
    """#2218: a rollup red only because `cancel-in-progress` cancelled the run is
    not a broken PR — proposing "fix the failing CI" would send the session after
    a branch that was never broken."""
    from server.github.pr_status import CheckDetail, PullRequestInfo
    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    superseded = (CheckDetail(name="server-tests (1)", conclusion="cancelled"),
                  CheckDetail(name="lint-and-test", conclusion="failure"))
    orch.pr_for = lambda repo, branch: PullRequestInfo(
        218, "u", "open", "failing", "fix the thing", checks_detail=superseded)
    orch._review_proposals[("o/r", 218)] = (time.monotonic(), None)
    assert orch._dispatch_proposal(rec) is None


@pytest.mark.asyncio
async def test_refresh_review_proposal_stores_text(monkeypatch):
    """After _refresh_review_proposal, _review_proposal returns the formatted string."""
    monkeypatch.setattr(orch_mod.dispatch, "fetch_unresolved_review_count", lambda repo, number: 2)

    orch = _make_orch()
    await orch._refresh_review_proposal("o/r", 7, "t")

    result = orch._review_proposal("o/r", 7, "t")
    assert result == "Address 2 review comments on PR #7: t"


@pytest.mark.asyncio
async def test_dispatch_proposal_suggests_own_pr_reviews(monkeypatch):
    """A passing PR with unresolved reviews returns a review proposal."""
    from server.github.pr_status import PullRequestInfo

    monkeypatch.setattr(orch_mod.dispatch, "fetch_unresolved_review_count", lambda repo, number: 3)

    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    # Passing PR
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "passing", "PR title")
    # Seed a fresh review cache with unresolved count
    orch._review_proposals[("o/r", 7)] = (time.monotonic(), "Address 3 review comments on PR #7: PR title")

    result = orch._dispatch_proposal(rec)
    assert result == "Address 3 review comments on PR #7: PR title"


def test_dispatch_proposal_failing_pr_wins_over_review():
    """Cascade priority: a failing PR proposal is returned, review not consulted."""
    from server.github.pr_status import PullRequestInfo

    orch = _make_orch()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/x")
    # Failing PR
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "failing", "PR title")
    # Seed review cache with unresolved reviews
    orch._review_proposals[("o/r", 7)] = (time.monotonic(), "Address 2 review comments on PR #7: PR title")

    result = orch._dispatch_proposal(rec)
    assert result == "Fix the failing CI on PR #7: PR title"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [AgentEngine.CLAUDE_CODE, AgentEngine.CODEX])
@pytest.mark.parametrize("separate_hosts", [False, True])
async def test_sessions_only_receive_their_own_suggestions(engine, separate_hosts, monkeypatch):
    """Same-repo sessions must not receive repo-wide work, even on separate daemons."""
    first = SessionOrchestrator()
    second = SessionOrchestrator() if separate_hosts else first
    for orch in (first, second):
        # Close any scheduled coroutine so a regression fails without hitting GitHub.
        orch._spawn = Mock(side_effect=lambda coro, **kw: coro.close())
        orch.resolve_transcript_path = AsyncMock(return_value="/fake/transcript")
    monkeypatch.setattr(orch_mod.os.path, "getmtime", lambda path: 123.0)

    for orch, sid, topic in ((first, "s1", "Calendar"), (second, "s2", "Android")):
        rec = _make_record(sid, repo="o/r")
        rec.engine = engine
        orch._records[sid] = rec
        context = f"User: {topic}\n\nAssistant: Finished {topic}"
        orch.transcript_events = AsyncMock(return_value=[
            ev.UserMessage(turn_id="t", text=topic), ev.TextDelta(turn_id="t", text=f"Finished {topic}"),
        ])
        signature = hashlib.sha1(context.encode()).hexdigest() if engine is AgentEngine.CODEX else 123.0
        own_suggestions = [f"Review {topic}"]
        orch._detached_suggestions[sid] = (signature, own_suggestions)

        assert await orch.latest_suggestions(sid) == own_suggestions
        orch._spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [AgentEngine.CLAUDE_CODE, AgentEngine.CODEX])
async def test_missing_transcript_does_not_offer_unrelated_work(engine):
    orch = SessionOrchestrator()
    rec = _make_record("s1", repo="o/r")
    rec.engine = engine
    orch._records[rec.id] = rec
    orch.resolve_transcript_path = AsyncMock(return_value=None)
    orch.transcript_events = AsyncMock(side_effect=RuntimeError("unavailable"))
    orch._spawn = Mock(side_effect=lambda coro, **kw: coro.close())

    assert await orch.latest_suggestions(rec.id) == []
    orch._spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [AgentEngine.CLAUDE_CODE, AgentEngine.CODEX])
async def test_own_pr_followup_survives_missing_transcript(engine):
    from server.github.pr_status import PullRequestInfo

    orch = SessionOrchestrator()
    rec = _record_with_branch("s1", repo="o/r", branch="feat/calendar")
    rec.engine = engine
    orch._records[rec.id] = rec
    orch.pr_for = lambda repo, branch: PullRequestInfo(7, "u", "open", "failing", "Calendar")
    orch.resolve_transcript_path = AsyncMock(return_value=None)
    orch.transcript_events = AsyncMock(side_effect=RuntimeError("unavailable"))

    assert await orch.latest_suggestions(rec.id) == ["Fix the failing CI on PR #7: Calendar"]
