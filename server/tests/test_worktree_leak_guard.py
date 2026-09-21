"""The conftest real-worktree destruction guard (see server/tests/conftest.py).

Regression lock for #1014: a `pytest server/tests/` run swept the developer's
live managed-worktree tree under ``~/GitHub``. The startup janitor builds
``live_cwds`` from the orchestrator's in-memory session registry, which in a test
process is empty — so every REAL session's worktree read as unowned and the
7-day dirty backlog was stash-evicted:

    worktree-janitor: stash failed, discarding path=.../KinDash/2ccac630 age_days=7.3

conftest fences the repo-root env vars into a temp dir (locked by
test_production_db_isolation.py), but that is a precondition a test can defeat.
These tests exercise the backstop underneath it: the guard must refuse — and
record — any sweep or removal outside the system temp dir, while leaving
legitimate tmp_path work untouched.

Hostile paths here are LITERALS, never built from ``Path.home()``: the local test
runbook runs the suite under ``env -i`` with an isolated HOME, which can itself
sit under the temp dir and would silently invert every assertion below.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from unittest.mock import patch

import pytest

from ccswitch import worktrees, worktrees_janitor

from server.tests.conftest import (
    _drain_real_worktree_violations,
    _fail_on_real_worktree_violations,
    _is_disposable_path,
)

# Stand-ins for the developer's live tree — the exact shape from the #1014 log.
# The worktree path is derived from `worktrees_base()` rather than spelled out,
# so it tracks the real managed layout instead of duplicating it here.
LIVE_REPO_ROOT = "/home/dev/GitHub"
LIVE_WORKTREE = str(
    worktrees.worktrees_base(LIVE_REPO_ROOT) / "KinDash" / "2ccac630")


class TestGuardRefusesTheRealTree:
    def test_sweep_of_a_live_repo_root_raises(self):
        """The #1014 call itself: sweeping a live root must raise before walking it."""
        with pytest.raises(RuntimeError, match="sweep_orphaned_worktrees"):
            worktrees_janitor.sweep_orphaned_worktrees(LIVE_REPO_ROOT, frozenset())
        assert _drain_real_worktree_violations()  # recorded, not just raised

    def test_remove_of_a_live_worktree_raises_before_running_any_git(self):
        """The destructive primitive refuses, and refuses *first* — no git
        subprocess runs, so the user's uncommitted work is never stashed or
        force-removed on the way to the error."""
        with patch.object(worktrees, "_run") as mock_run:
            with pytest.raises(RuntimeError, match="remove_worktree"):
                worktrees.remove_worktree(LIVE_WORKTREE)
        mock_run.assert_not_called()
        assert _drain_real_worktree_violations()

    def test_force_removal_is_refused_too(self):
        """``force=True`` is the janitor's max-age eviction path — the one that
        discards uncommitted work — so it must not slip past the guard."""
        with pytest.raises(RuntimeError, match="remove_worktree"):
            worktrees.remove_worktree(LIVE_WORKTREE, force=True)
        assert _drain_real_worktree_violations()

    def test_janitor_module_reference_is_guarded_too(self):
        """The janitor binds ``remove_worktree`` as a module-level name at import
        time, so that name must point at the guarded callable, not the raw one —
        otherwise its own eviction path bypasses the guard entirely."""
        assert getattr(
            worktrees_janitor.remove_worktree, "_is_real_worktree_guard", False)
        assert getattr(worktrees.remove_worktree, "_is_real_worktree_guard", False)

    def test_orchestrator_repo_root_fallback_bypasses_the_env_fence(self):
        """The hole the env fence cannot close: ``SessionOrchestrator.__init__``
        falls back to ``~/GitHub`` when get_settings() raises, handing the janitor
        the developer's live tree whatever the fenced repo root says."""
        from server.config import get_settings
        from server.sessions.orchestrator import SessionOrchestrator

        fenced_root = get_settings().repo_root
        with patch("server.config.get_settings", side_effect=RuntimeError("boom")):
            orch = SessionOrchestrator(repo_root=None)
        assert orch._repo_root == os.path.expanduser("~/GitHub")
        assert orch._repo_root != fenced_root

    def test_orchestrator_sweep_of_a_live_root_is_refused(self):
        """And the guard catches it one layer down, at the sweep the janitor
        loop actually performs."""
        from server.sessions.orchestrator import SessionOrchestrator

        orch = SessionOrchestrator(repo_root=LIVE_REPO_ROOT)
        with pytest.raises(RuntimeError, match="sweep_orphaned_worktrees"):
            orch._janitor_sweep_once()
        assert _drain_real_worktree_violations()


class TestGuardLeavesLegitimateWorkAlone:
    def test_sweep_under_tmp_path_runs_normally(self, tmp_path):
        counts = worktrees_janitor.sweep_orphaned_worktrees(str(tmp_path), frozenset())
        assert counts["scanned"] == 0
        assert _drain_real_worktree_violations() == []

    def test_remove_of_a_git_worktree_under_tmp_path_succeeds(self, tmp_path):
        """A genuine git worktree in tmp_path is really removed — the guard is
        scoped to location, it does not stub the primitive out."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "f.txt").write_text("hi")
        git("add", "f.txt")
        git("commit", "-m", "init")

        wt = tmp_path / "wt"
        assert git("worktree", "add", str(wt), "-b", "side").returncode == 0
        assert wt.is_dir()

        assert worktrees.remove_worktree(str(wt)) is True
        assert not wt.exists()
        assert _drain_real_worktree_violations() == []


class TestDisposablePathPredicate:
    @pytest.mark.parametrize(
        "path", [LIVE_REPO_ROOT, LIVE_WORKTREE, "/srv/repos", "/", None, 5])
    def test_paths_outside_the_temp_dir_are_not_disposable(self, path):
        assert _is_disposable_path(path) is False

    def test_tmp_path_and_gettempdir_are_disposable(self, tmp_path):
        assert _is_disposable_path(tmp_path) is True
        assert _is_disposable_path(tempfile.gettempdir()) is True


def test_a_swallowed_refusal_still_fails_the_test():
    """Mutation check: recording→failure is the step that makes a leak visible.

    ``_worktree_janitor_loop`` swallows the guard's RuntimeError
    (``except Exception: log.exception(...)``), so raising alone would stop the
    damage and leave the test green — the original incident's exact shape.
    Swallow a refusal the way the loop does, then run the autouse fixture's
    teardown check and assert it fails anyway. ``pytest.fail`` raises ``Failed``,
    a BaseException — which is also why the janitor's ``except Exception`` can
    never swallow the resulting failure the way it swallows the RuntimeError.
    """
    try:
        worktrees_janitor.sweep_orphaned_worktrees(LIVE_REPO_ROOT, frozenset())
    except Exception:
        pass  # exactly what the janitor loop does

    with pytest.raises(pytest.fail.Exception, match="reached the real worktree tree"):
        _fail_on_real_worktree_violations()

    assert _drain_real_worktree_violations() == []  # the check drained it


def test_no_violations_means_no_failure():
    """The teardown check must be silent on a clean run — a guard that always
    failed would surface here rather than as noise across the whole suite."""
    assert _drain_real_worktree_violations() == []
    _fail_on_real_worktree_violations()  # does not raise
