"""Tests for server.api.sessions._resolve_repo's fallback to the stored repo.

The session daemon can run on a different host from the edge process (see
#712). ``_resolve_repo`` used to fall back to ``record.repo`` only when
``cwd`` was empty — but on a cross-host deployment ``cwd`` is *set* (it points
to a real path on the daemon host) while being completely unreadable from the
edge process's filesystem, so the live git read silently returns None and the
UI renders every session as "Scratch" even though the DB has the correct
``repo`` value.

The fix: fall back to ``record.repo`` whenever the live read yields nothing —
None/empty *or* raises — not only when ``cwd`` itself is falsy. A genuine
scratch session (real dir, no git remote) must still resolve to None, because
in that case ``record.repo`` is itself None.
"""
from __future__ import annotations

import asyncio
import types

from ccswitch import gitrepos
from server.api import sessions as sessions_api


def _make_record(cwd, repo):
    return types.SimpleNamespace(cwd=cwd, repo=repo)


def test_live_read_none_falls_back_to_stored_repo(monkeypatch):
    """The bug case: cwd is set but unreadable from this process (live read
    returns None), while the DB has the correct stored repo. Must NOT render
    as Scratch."""
    monkeypatch.setattr(gitrepos, "repo_at_path", lambda cwd: None)
    record = _make_record(
        cwd="/home/tony/GitHub/.ccswitch/worktrees/KinDash/ef3c4da3",
        repo="cairnea/KinDash",
    )
    result = asyncio.run(sessions_api._resolve_repo(record))
    assert result == "cairnea/KinDash"


def test_live_read_wins_over_stored_repo(monkeypatch):
    """When the live read succeeds, prefer it over the stored value — it
    reflects what's actually checked out (e.g. after a branch switch)."""
    monkeypatch.setattr(gitrepos, "repo_at_path", lambda cwd: "live/repo")
    record = _make_record(cwd="/some/cwd", repo="stored/repo")
    result = asyncio.run(sessions_api._resolve_repo(record))
    assert result == "live/repo"


def test_genuine_scratch_session_still_resolves_to_none(monkeypatch):
    """A real scratch dir with no git remote and no stored repo must still
    render as Scratch (None), not be coerced into some sentinel value."""
    monkeypatch.setattr(gitrepos, "repo_at_path", lambda cwd: None)
    record = _make_record(cwd="/some/scratch/dir", repo=None)
    result = asyncio.run(sessions_api._resolve_repo(record))
    assert result is None


def test_no_cwd_returns_stored_repo_unchanged(monkeypatch):
    """Unchanged behavior: no cwd at all skips the live read entirely."""
    def _boom(cwd):
        raise AssertionError("repo_at_path should not be called when cwd is falsy")

    monkeypatch.setattr(gitrepos, "repo_at_path", _boom)
    record = _make_record(cwd=None, repo="stored/repo")
    result = asyncio.run(sessions_api._resolve_repo(record))
    assert result == "stored/repo"


def test_live_read_raises_falls_back_to_stored_repo(monkeypatch):
    """A broken git binary or permission error must not 500 the sessions
    list — it should degrade to the stored repo instead of propagating."""
    def _boom(cwd):
        raise RuntimeError("git binary not found")

    monkeypatch.setattr(gitrepos, "repo_at_path", _boom)
    record = _make_record(cwd="/some/cwd", repo="stored/repo")
    result = asyncio.run(sessions_api._resolve_repo(record))
    assert result == "stored/repo"
