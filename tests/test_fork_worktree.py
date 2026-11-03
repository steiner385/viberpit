"""Fork worktrees (D-8): a child worktree at the source's HEAD carrying its uncommitted
work, while the source stays byte-identical. Real temp repos throughout."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from viberpit import brand, worktrees
from viberpit.worktrees import ForkWorktreeError, create_fork_worktree

SID = "abcd1234-0000-4000-8000-000000000000"


def _git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "sub").mkdir()
    (path / "a.txt").write_text("a1\n")
    (path / "sub" / "b.txt").write_text("b1\n")
    (path / "d.txt").write_text("doomed\n")
    (path / "touched.txt").write_text("same\n")
    (path / ".gitignore").write_text("*.log\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    (path / "a.txt").write_text("a2\n")
    _git(path, "commit", "-qam", "second")          # committed change


def _dirty(path: Path) -> None:
    (path / "a.txt").write_text("a3 staged\n")
    (path / "c.txt").write_text("new staged\n")
    _git(path, "add", "a.txt", "c.txt")              # staged
    (path / "sub" / "b.txt").write_text("b2 unstaged\n")   # unstaged
    (path / "d.txt").unlink()                        # unstaged deletion
    (path / "u.txt").write_text("untracked\n")
    (path / "newdir").mkdir()
    (path / "newdir" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(path / "newdir" / "run.sh", 0o750)
    (path / "debug.log").write_text("ignored\n")
    # Stat-dirty but content-identical: a plain `git stash create` would refresh
    # (rewrite) the source index for this file.
    st = os.stat(path / "touched.txt")
    os.utime(path / "touched.txt", (st.st_atime + 5, st.st_mtime + 5))


def _snapshot(path: Path) -> dict[str, object]:
    """Index bytes, HEAD, refs, porcelain status and every working-tree file."""
    git_dir = path / ".git"
    snap: dict[str, object] = {
        "index": hashlib.sha256((git_dir / "index").read_bytes()).hexdigest(),
        "head_file": (git_dir / "HEAD").read_bytes(),
        "head": _git(path, "rev-parse", "HEAD"),
        # The ref store is shared with the child, whose new branch is expected; every
        # other ref (and no refs/stash) must be unchanged.
        "refs": [line for line in _git(
            path, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
            if not line.startswith(f"refs/heads/{brand.BRANCH_PREFIX}fork-")],
    }
    files: dict[str, tuple[int, str]] = {}
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if not (root == str(path) and d == ".git")]
        for name in names:
            full = os.path.join(root, name)
            st = os.lstat(full)
            rel = os.path.relpath(full, path)
            if stat.S_ISREG(st.st_mode):
                files[rel] = (st.st_mode, hashlib.sha256(Path(full).read_bytes()).hexdigest())
            else:
                files[rel] = (st.st_mode, "")
    snap["files"] = files
    # Hash the index BEFORE status, and never let status refresh it.
    snap["status"] = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v2", "-z"],
        check=True, capture_output=True).stdout
    return snap


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _make_repo(r)
    return r


def _branches(repo: Path) -> list[str]:
    return _git(repo, "branch", "--format=%(refname:short)").split()


def _registered(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain")


# -- branch naming -------------------------------------------------------------------

def test_fork_branch_name_follows_the_session_prefix_rule() -> None:
    name = worktrees.fork_branch_name(SID, 2)
    assert name == f"{brand.BRANCH_PREFIX}fork-abcd1234-2"
    assert brand.is_session_branch(name)


def test_next_fork_branch_skips_existing(repo: Path) -> None:
    _git(repo, "branch", worktrees.fork_branch_name(SID, 1))
    assert worktrees.next_fork_branch(str(repo), SID) == worktrees.fork_branch_name(SID, 2)


@pytest.mark.parametrize("bad", ["-x", "feature", "main"])
def test_rejects_a_branch_outside_the_session_prefix(repo: Path, tmp_path: Path,
                                                    bad: str) -> None:
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError):
        create_fork_worktree(str(repo), branch=bad, dest=str(dest))
    assert not dest.exists()


# -- the fork itself -----------------------------------------------------------------

def test_child_carries_every_kind_of_change_and_source_is_untouched(
        repo: Path, tmp_path: Path) -> None:
    _dirty(repo)
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    branch = worktrees.fork_branch_name(SID, 1)

    info = create_fork_worktree(str(repo / "sub"), branch=branch, dest=str(dest))

    assert _snapshot(repo) == before
    assert info.path == str(dest)
    assert info.cwd == str(dest / "sub")          # mirrors the source's subdir
    assert info.branch == branch
    assert info.repo == str(repo)
    assert _git(dest, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch
    # committed + staged
    assert (dest / "a.txt").read_text() == "a3 staged\n"
    assert set(_git(dest, "diff", "--cached", "--name-only").split()) == {"a.txt", "c.txt"}
    # unstaged
    assert (dest / "sub" / "b.txt").read_text() == "b2 unstaged\n"
    assert not (dest / "d.txt").exists()
    assert set(_git(dest, "diff", "--name-only").split()) == {"sub/b.txt", "d.txt"}
    # untracked, mode preserved; ignored not copied
    assert (dest / "u.txt").read_text() == "untracked\n"
    assert stat.S_IMODE(os.stat(dest / "newdir" / "run.sh").st_mode) == 0o750
    assert not (dest / "debug.log").exists()
    assert set(_git(dest, "ls-files", "--others", "--exclude-standard").split()) == {
        "u.txt", "newdir/run.sh"}
    # no stash entry was recorded anywhere
    assert "refs/stash" not in _git(repo, "for-each-ref")


def test_clean_source_forks_to_a_clean_child(repo: Path, tmp_path: Path) -> None:
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    info = create_fork_worktree(str(repo), branch=worktrees.fork_branch_name(SID, 1),
                                dest=str(dest))
    assert _snapshot(repo) == before
    assert info.cwd == str(dest)
    assert _git(dest, "status", "--porcelain") == ""


def test_fork_from_a_session_worktree_source(repo: Path, tmp_path: Path) -> None:
    """The source is normally itself a linked worktree, not the clone."""
    src = tmp_path / "src-wt"
    _git(repo, "worktree", "add", "-q", "-b", "viberpit/src", str(src))
    _dirty(src)
    before = _snapshot_linked(src)
    dest = tmp_path / "wt"
    info = create_fork_worktree(str(src), branch=worktrees.fork_branch_name(SID, 1),
                                dest=str(dest))
    assert _snapshot_linked(src) == before
    assert info.repo == str(repo)
    assert (dest / "u.txt").read_text() == "untracked\n"
    assert (dest / "a.txt").read_text() == "a3 staged\n"


def _snapshot_linked(path: Path) -> tuple[bytes, str, bytes]:
    index = Path(_git(path, "rev-parse", "--path-format=absolute",
                      "--git-path", "index").strip())
    idx = index.read_bytes()
    status = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v2", "-z"],
        check=True, capture_output=True).stdout
    return idx, _git(path, "rev-parse", "HEAD"), status


def test_symlink_and_fifo_untracked_entries_are_skipped(repo: Path, tmp_path: Path) -> None:
    (repo / "outside.txt").write_text("x\n")
    os.symlink("/etc/passwd", repo / "link")
    os.mkfifo(repo / "pipe")
    (repo / "keep.txt").write_text("keep\n")
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    create_fork_worktree(str(repo), branch=worktrees.fork_branch_name(SID, 1),
                         dest=str(dest))
    assert _snapshot(repo) == before
    assert (dest / "keep.txt").read_text() == "keep\n"
    assert not os.path.lexists(dest / "link")
    assert not os.path.lexists(dest / "pipe")


# -- failure cleanup -----------------------------------------------------------------

def _assert_no_half_fork(repo: Path, dest: Path, branch: str) -> None:
    assert not dest.exists()
    assert branch not in _branches(repo)
    assert str(dest) not in _registered(repo)


def test_untracked_over_the_size_bound_fails_and_cleans_up(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees, "FORK_MAX_UNTRACKED_BYTES", 8)
    (repo / "big.txt").write_text("0123456789\n")
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    branch = worktrees.fork_branch_name(SID, 1)
    with pytest.raises(ForkWorktreeError) as ei:
        create_fork_worktree(str(repo), branch=branch, dest=str(dest))
    assert ei.value.code == "FORK_UNTRACKED_TOO_LARGE"
    assert _snapshot(repo) == before
    _assert_no_half_fork(repo, dest, branch)


def test_failure_partway_removes_the_half_made_worktree(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dirty(repo)
    before = _snapshot(repo)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(worktrees, "_copy_untracked", boom)
    dest = tmp_path / "wt"
    branch = worktrees.fork_branch_name(SID, 1)
    with pytest.raises(ForkWorktreeError):
        create_fork_worktree(str(repo), branch=branch, dest=str(dest))
    assert _snapshot(repo) == before
    _assert_no_half_fork(repo, dest, branch)


def test_existing_dest_is_refused_and_left_alone(repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "wt"
    dest.mkdir()
    (dest / "precious").write_text("mine\n")
    branch = worktrees.fork_branch_name(SID, 1)
    with pytest.raises(ForkWorktreeError):
        create_fork_worktree(str(repo), branch=branch, dest=str(dest))
    assert (dest / "precious").read_text() == "mine\n"
    assert branch not in _branches(repo)


def test_existing_branch_is_refused_and_left_alone(repo: Path, tmp_path: Path) -> None:
    branch = worktrees.fork_branch_name(SID, 1)
    _git(repo, "branch", branch)
    tip = _git(repo, "rev-parse", branch)
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError):
        create_fork_worktree(str(repo), branch=branch, dest=str(dest))
    assert not dest.exists()
    assert _git(repo, "rev-parse", branch) == tip    # not deleted by cleanup


def test_non_repo_source_raises(tmp_path: Path) -> None:
    with pytest.raises(ForkWorktreeError):
        create_fork_worktree(str(tmp_path), branch=worktrees.fork_branch_name(SID, 1),
                             dest=str(tmp_path / "wt"))


def test_copy_enforces_the_bound_on_bytes_actually_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that grows after the plan's lstat still cannot blow the bound."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "grew.txt").write_text("0123456789\n")
    monkeypatch.setattr(worktrees, "FORK_MAX_UNTRACKED_BYTES", 4)
    with pytest.raises(ForkWorktreeError) as ei:
        worktrees._copy_untracked(str(src), str(dst), [b"grew.txt"])
    assert ei.value.code == "FORK_UNTRACKED_TOO_LARGE"
