"""Recovery refs and worktree recreation (#2913, #2914).

Spec: docs/superpowers/specs/2026-10-11-restart-worktree-data-safety-design.md.
Every test runs against real git repos under ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from viberpit import worktrees


def _git(*args: str, cwd: str | Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    (path / "tracked.txt").write_text("one\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "init", cwd=path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    _make_repo(r)
    return r


@pytest.fixture
def wt(repo: Path, tmp_path: Path) -> worktrees.WorktreeInfo:
    _git("branch", "feature", cwd=repo)
    return worktrees.create_worktree(str(repo), "feature", "abcd1234-0000", str(tmp_path))


def _ls_tree(repo: Path, ref: str) -> dict[str, str]:
    out = _git("ls-tree", "-r", ref, cwd=repo)
    files = {}
    for line in out.splitlines():
        meta, name = line.split("\t", 1)
        files[name] = _git("cat-file", "-p", meta.split()[2], cwd=repo)
    return files


def _dirty(path: Path) -> None:
    (path / "tracked.txt").write_text("unstaged edit\n")       # unstaged
    (path / "staged.txt").write_text("staged\n")
    _git("add", "staged.txt", cwd=path)                         # staged
    (path / "untracked.txt").write_text("untracked\n")          # untracked


# ---------------------------------------------------------------------------
# snapshot_recovery_ref
# ---------------------------------------------------------------------------

def test_snapshot_captures_staged_unstaged_and_untracked(wt, repo):
    path = Path(wt.path)
    _dirty(path)
    ref = worktrees.snapshot_recovery_ref(wt.path, "sess-1", reason="test")
    assert ref is not None
    assert ref.startswith(f"{worktrees.RECOVERY_REF_NS}/sess-1/")
    files = _ls_tree(repo, ref)
    assert files["tracked.txt"] == "unstaged edit"
    assert files["staged.txt"] == "staged"
    assert files["untracked.txt"] == "untracked"
    # Parent is HEAD, so the branch tip is preserved too.
    assert _git("rev-parse", f"{ref}^", cwd=repo) == _git("rev-parse", "HEAD", cwd=path)
    assert "viberpit recovery: test" in _git("log", "-1", "--format=%B", ref, cwd=repo)


def test_snapshot_leaves_index_and_worktree_byte_identical(wt):
    path = Path(wt.path)
    _dirty(path)
    index = Path(_git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=path))
    before_index = index.read_bytes()
    before_status = _git("status", "--porcelain", cwd=path)
    before_files = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}

    assert worktrees.snapshot_recovery_ref(wt.path, "sess-1", reason="test") is not None

    assert index.read_bytes() == before_index
    assert _git("status", "--porcelain", cwd=path) == before_status
    assert {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()} == before_files


def test_snapshot_of_clean_tree_returns_none(wt, repo):
    assert worktrees.snapshot_recovery_ref(wt.path, "sess-1", reason="test") is None
    assert worktrees.recovery_refs(str(repo), "sess-1") == []


def test_snapshot_keeps_newest_five(wt, repo):
    path = Path(wt.path)
    made = []
    for i in range(7):
        (path / "tracked.txt").write_text(f"edit {i}\n")
        ref = worktrees.snapshot_recovery_ref(wt.path, "sess-1", reason=f"r{i}")
        assert ref is not None
        made.append(ref)
    kept = worktrees.recovery_refs(str(repo), "sess-1")
    assert kept == list(reversed(made))[:worktrees.RECOVERY_KEEP]
    assert len(kept) == 5


def test_snapshot_never_raises(tmp_path):
    assert worktrees.snapshot_recovery_ref(str(tmp_path / "nope"), "s", reason="x") is None


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------

def test_force_remove_leaves_recovery_ref(wt, repo):
    _dirty(Path(wt.path))
    assert worktrees.remove_worktree(wt.path, force=True, session_id="sess-2")
    refs = worktrees.recovery_refs(str(repo), "sess-2")
    assert refs, "a forced removal must snapshot the dirty tree first"
    assert _ls_tree(repo, refs[-1])["untracked.txt"] == "untracked"


def test_clean_close_with_unpushed_commits_keeps_branch_tip(repo, tmp_path):
    # A child session branch (only those are branch -D'd on removal).
    _git("branch", "feature", cwd=repo)
    _git("checkout", "feature", cwd=repo)       # busy → create_worktree makes a child
    info = worktrees.create_worktree(str(repo), "feature", "beef0001-0000", str(tmp_path))
    assert info.is_child
    path = Path(info.path)
    (path / "work.txt").write_text("committed but never pushed\n")
    _git("add", "-A", cwd=path)
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "work", cwd=path)
    tip = _git("rev-parse", "HEAD", cwd=path)

    assert worktrees.remove_worktree(info.path, session_id="sess-3")
    gone = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                           f"refs/heads/{info.branch}"], capture_output=True)
    assert gone.returncode != 0, "the clean close still deletes the session branch"
    refs = worktrees.recovery_refs(str(repo), "sess-3")
    assert len(refs) == 1 and refs[0].endswith("-branch")
    assert _git("rev-parse", refs[0], cwd=repo) == tip


def test_clean_close_without_new_commits_writes_no_ref(repo, tmp_path):
    _git("branch", "feature", cwd=repo)
    _git("checkout", "feature", cwd=repo)
    info = worktrees.create_worktree(str(repo), "feature", "beef0002-0000", str(tmp_path))
    assert worktrees.remove_worktree(info.path, session_id="sess-4")
    assert worktrees.recovery_refs(str(repo), "sess-4") == []


# ---------------------------------------------------------------------------
# recreate_worktree
# ---------------------------------------------------------------------------

def test_recreate_missing_worktree_from_its_branch(wt, repo):
    import shutil
    path = wt.path
    shutil.rmtree(path)                           # gone from disk, registration stale
    assert worktrees.recreate_worktree(str(repo), path, "feature")
    assert worktrees.current_branch(path) == "feature"
    assert (Path(path) / "README.md").read_text() == "hello\n"


def test_recreate_refuses_when_branch_absent(repo, tmp_path):
    dest = tmp_path / "wts" / "x"
    assert not worktrees.recreate_worktree(str(repo), str(dest), "no-such-branch")
    assert not dest.exists()


def test_recreate_never_touches_an_existing_path(wt, repo):
    marker = Path(wt.path) / "keep.txt"
    marker.write_text("mine\n")
    assert not worktrees.recreate_worktree(str(repo), wt.path, "feature")
    assert marker.read_text() == "mine\n"
