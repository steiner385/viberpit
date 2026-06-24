"""The PR badge joins a session to its GitHub PR by (repo, branch). The branch
must be read LIVE from the session's worktree HEAD — not the value stored at
session creation — because ccswitch's own completion workflow checks out a fresh
``feat/*``/``fix/*`` branch in the same worktree before opening the PR. Mirrors
``_resolve_repo``, which already reads the repo live from the cwd.
"""
from __future__ import annotations

import subprocess

import pytest

from server.api.sessions import _resolve_branch, _resolve_pr
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _rec(cwd, branch):
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd=cwd, repo="o/r", branch=branch,
    )


@pytest.mark.asyncio
async def test_resolve_branch_reads_live_worktree_head(tmp_path):
    """A worktree that has moved off its creation branch resolves to its live HEAD."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "feat/global-mutation-error-toast")
    # The stored branch is the stale per-session creation branch.
    rec = _rec(str(repo), "ccswitch/master-a047d232")
    assert await _resolve_branch(rec) == "feat/global-mutation-error-toast"


@pytest.mark.asyncio
async def test_resolve_branch_falls_back_to_stored_when_no_cwd():
    """No working dir (e.g. before a worktree exists) → fall back to the stored branch."""
    rec = _rec("", "ccswitch/master-x")
    assert await _resolve_branch(rec) == "ccswitch/master-x"


@pytest.mark.asyncio
async def test_resolve_branch_falls_back_to_stored_on_detached_or_non_git(tmp_path):
    """A non-git/detached cwd can't be read live → fall back to the stored branch.

    The API and the poller must resolve the SAME branch (else the poller caches a
    PR under one key and the API looks it up under another), so both degrade to the
    stored value identically via ``worktrees.live_branch``.
    """
    plain = tmp_path / "scratch"
    plain.mkdir()
    rec = _rec(str(plain), "ccswitch/master-x")
    assert await _resolve_branch(rec) == "ccswitch/master-x"


@pytest.mark.asyncio
async def test_resolve_branch_none_when_no_branch_anywhere(tmp_path):
    """A scratch session (non-git cwd, no stored branch) resolves to None."""
    plain = tmp_path / "scratch"
    plain.mkdir()
    rec = _rec(str(plain), None)
    assert await _resolve_branch(rec) is None


@pytest.mark.asyncio
async def test_resolve_pr_uses_passed_branch_not_stale_record():
    """``_resolve_pr`` keys the PR lookup on the branch it is given, not record.branch."""
    class _Orch:
        def pr_for(self, repo, branch):
            return PullRequestInfo(220, "u", "open", "none", "t") if branch == "feat/foo" else None

    rec = _rec("/tmp/wt", "ccswitch/master-stale")
    assert (await _resolve_pr(_Orch(), rec, "feat/foo")).number == 220
    assert await _resolve_pr(_Orch(), rec, "ccswitch/master-stale") is None
