"""``worktrees.ensure_local_branch`` (spec 002 FR-204, #949): a host adopting a session
off a dead host has, at best, origin's copy of the session's branch — the recreation
of its worktree needs a LOCAL ref. Against a local bare origin, never GitHub."""
from __future__ import annotations

import subprocess
from pathlib import Path

from viberpit import worktrees


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _clone_with_remote_only_branch(root: Path) -> Path:
    """A clone whose origin has ``feat`` but which never fetched it."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin), "-b", "main"], check=True,
                   capture_output=True)
    seed = root / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=str(seed))
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init",
         cwd=str(seed))
    _git("remote", "add", "origin", str(origin), cwd=str(seed))
    _git("push", "-u", "origin", "main", cwd=str(seed))
    _git("branch", "feat", cwd=str(seed))
    _git("push", "origin", "feat", cwd=str(seed))
    clone = root / "clone"
    _git("clone", "-q", "--branch", "main", "--single-branch", str(origin), str(clone),
         cwd=str(root))
    return clone


def test_fetches_a_remote_only_branch_and_creates_it_locally(tmp_path):
    clone = _clone_with_remote_only_branch(tmp_path)
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "-q",
                           "refs/heads/feat"], capture_output=True).returncode != 0
    assert worktrees.ensure_local_branch(str(clone), "feat") is True
    assert _git("rev-parse", "refs/heads/feat", cwd=str(clone)) == \
        _git("rev-parse", "refs/remotes/origin/feat", cwd=str(clone))
    # Now the recreation that needs it works.
    dest = tmp_path / "wt"
    assert worktrees.recreate_worktree(str(clone), str(dest), "feat") is True
    assert (dest / ".git").exists()


def test_leaves_an_existing_local_branch_alone(tmp_path):
    clone = _clone_with_remote_only_branch(tmp_path)
    _git("branch", "feat", "main", cwd=str(clone))          # local, at a DIFFERENT tip
    before = _git("rev-parse", "refs/heads/feat", cwd=str(clone))
    assert worktrees.ensure_local_branch(str(clone), "feat") is True
    assert _git("rev-parse", "refs/heads/feat", cwd=str(clone)) == before


def test_false_when_the_branch_exists_nowhere(tmp_path):
    clone = _clone_with_remote_only_branch(tmp_path)
    assert worktrees.ensure_local_branch(str(clone), "nope") is False
    assert worktrees.ensure_local_branch(str(clone), "-flag") is False
    assert worktrees.ensure_local_branch(str(tmp_path / "not-a-repo"), "feat") is False
