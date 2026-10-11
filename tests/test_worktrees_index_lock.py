"""Stale worktree ``index.lock`` cleanup on re-attach (#2910 item 4).

A daemon restart kills a git process mid-commit inside a session worktree and leaves
``<gitdir>/index.lock`` behind; git never removes a dead process's lock, so every later
commit fails until it is cleared.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from viberpit import worktrees
from viberpit.gitrepos import STALE_LOCK_AGE_S


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "init", cwd=path)


def _lock(gitdir: Path, *, age_s: float) -> Path:
    lock = gitdir / "index.lock"
    lock.write_text("")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    return lock


@pytest.fixture
def no_owner(monkeypatch):
    monkeypatch.setattr(worktrees, "_git_pids_in", lambda *_paths: set())


def test_old_orphaned_lock_is_removed(tmp_path, no_owner, caplog):
    repo = tmp_path / "repo"
    _make_repo(repo)
    lock = _lock(repo / ".git", age_s=STALE_LOCK_AGE_S + 30)

    with caplog.at_level("INFO", logger="viberpit.worktrees"):
        removed = worktrees.clear_stale_index_lock(str(repo))

    assert removed == str(lock.resolve())
    assert not lock.exists()
    assert "worktree.stale_index_lock_removed" in caplog.text


def test_lock_found_from_a_subdirectory_cwd(tmp_path, no_owner):
    repo = tmp_path / "repo"
    _make_repo(repo)
    sub = repo / "pkg"
    sub.mkdir()
    lock = _lock(repo / ".git", age_s=STALE_LOCK_AGE_S + 30)

    assert worktrees.clear_stale_index_lock(str(sub)) == str(lock.resolve())
    assert not lock.exists()


def test_young_lock_is_kept(tmp_path, no_owner):
    repo = tmp_path / "repo"
    _make_repo(repo)
    lock = _lock(repo / ".git", age_s=1)

    assert worktrees.clear_stale_index_lock(str(repo)) is None
    assert lock.exists()


def test_lock_with_live_git_owner_is_kept(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo)
    lock = _lock(repo / ".git", age_s=STALE_LOCK_AGE_S + 30)
    monkeypatch.setattr(worktrees, "_git_pids_in", lambda *_paths: {4242})

    assert worktrees.clear_stale_index_lock(str(repo)) is None
    assert lock.exists()


def test_unknowable_owner_scan_keeps_the_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo)
    lock = _lock(repo / ".git", age_s=STALE_LOCK_AGE_S + 30)
    monkeypatch.setattr(worktrees, "_git_pids_in", lambda *_paths: None)

    assert worktrees.clear_stale_index_lock(str(repo)) is None
    assert lock.exists()


def test_symlinked_lock_is_kept(tmp_path, no_owner):
    repo = tmp_path / "repo"
    _make_repo(repo)
    target = tmp_path / "elsewhere"
    target.write_text("")
    old = time.time() - STALE_LOCK_AGE_S - 30
    os.utime(target, (old, old))
    (repo / ".git" / "index.lock").symlink_to(target)

    assert worktrees.clear_stale_index_lock(str(repo)) is None
    assert target.exists()
    assert (repo / ".git" / "index.lock").is_symlink()


def test_linked_worktree_resolves_gitdir_from_dot_git_file(tmp_path, no_owner):
    repo = tmp_path / "repo"
    _make_repo(repo)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-b", "feat", str(wt), cwd=repo)
    assert (wt / ".git").is_file()
    wt_gitdir = repo / ".git" / "worktrees" / "wt"
    lock = _lock(wt_gitdir, age_s=STALE_LOCK_AGE_S + 30)
    main_lock = _lock(repo / ".git", age_s=STALE_LOCK_AGE_S + 30)

    assert worktrees.clear_stale_index_lock(str(wt)) == str(lock.resolve())
    assert not lock.exists()
    # Only the worktree's own index lock — never the main checkout's.
    assert main_lock.exists()


def test_no_lock_returns_none(tmp_path, no_owner):
    repo = tmp_path / "repo"
    _make_repo(repo)
    assert worktrees.clear_stale_index_lock(str(repo)) is None


def test_non_git_dir_returns_none(tmp_path, no_owner):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktrees.clear_stale_index_lock(str(plain)) is None


def test_missing_cwd_returns_none(tmp_path, no_owner):
    assert worktrees.clear_stale_index_lock(str(tmp_path / "gone")) is None


def test_git_pids_in_finds_a_live_git_process_by_cwd(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    # `git cat-file --batch` blocks on stdin, so it is a live `git` whose cwd is the repo.
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo,
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    try:
        deadline = time.time() + 5
        pids: set[int] | None = set()
        while time.time() < deadline:
            pids = worktrees._git_pids_in(str(repo))
            if pids:
                break
            time.sleep(0.05)
        assert pids is not None and proc.pid in pids
        assert proc.pid not in (worktrees._git_pids_in(str(tmp_path / "other")) or set())
    finally:
        proc.kill()
        proc.wait()
