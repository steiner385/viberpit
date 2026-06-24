"""The PR-status poller must query GitHub for each live session's CURRENT worktree
branch, not the branch stored at session creation. ccswitch sessions start on a
per-session ``ccswitch/master-<short>`` isolation branch and later check out a
``feat/*``/``fix/*`` branch (the real PR head) in the same worktree — so the
stored branch goes stale and the poll would miss the PR. ``_pr_pairs`` resolves
the branch live from the worktree.
"""
from __future__ import annotations

import pytest

from server.sessions.types import SessionRecord


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    return orch


def _rec(sid, cwd, repo, branch):
    return SessionRecord(
        id=sid, name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd=cwd, repo=repo, branch=branch,
    )


@pytest.mark.asyncio
async def test_pr_pairs_uses_live_worktree_branch(monkeypatch):
    """A session whose worktree moved to feat/foo is polled for (repo, feat/foo)."""
    orch = _make_orch()
    orch._records = {"s1": _rec("s1", "/tmp/wt", "o/r", "ccswitch/master-stale")}

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.worktrees, "current_branch", lambda p: "feat/foo")

    pairs = await orch._pr_pairs()
    assert pairs == [("o/r", "feat/foo")]


@pytest.mark.asyncio
async def test_pr_pairs_falls_back_to_stored_branch_when_no_live(monkeypatch):
    """No live branch resolvable (detached) → fall back to the stored branch."""
    orch = _make_orch()
    orch._records = {"s1": _rec("s1", "/tmp/wt", "o/r", "feat/stored")}

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.worktrees, "current_branch", lambda p: None)

    pairs = await orch._pr_pairs()
    assert pairs == [("o/r", "feat/stored")]


@pytest.mark.asyncio
async def test_pr_pairs_skips_sessions_without_repo(monkeypatch):
    """A no-repo (scratch) session contributes no pair even if it has a cwd."""
    orch = _make_orch()
    orch._records = {"s1": _rec("s1", "/tmp/scratch", None, None)}

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.worktrees, "current_branch", lambda p: None)

    assert await orch._pr_pairs() == []
