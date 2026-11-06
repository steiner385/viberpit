"""Fork worktrees (D-8): a child worktree at the source's HEAD carrying its uncommitted
work, while the source stays byte-identical. Real temp repos throughout."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from viberpit import brand, worktrees
from viberpit.worktree_diff import merge_driver_overrides
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


def _fork(source: Path | str, dest: Path, sid: str = SID) -> worktrees.ForkWorktreeInfo:
    return create_fork_worktree(str(source), source_session_id=sid, dest=str(dest))


# -- branch naming -------------------------------------------------------------------

def test_fork_branch_name_follows_the_session_prefix_rule() -> None:
    name = worktrees.fork_branch_name(SID, 2)
    assert name == f"{brand.BRANCH_PREFIX}fork-abcd1234-2"
    assert brand.is_session_branch(name)


def test_next_fork_branch_takes_the_first_free_n(repo: Path) -> None:
    for n in (1, 3):
        _git(repo, "branch", worktrees.fork_branch_name(SID, n))
    _git(repo, "branch", worktrees.fork_branch_name(SID, 2) + "x")   # not a number
    assert worktrees._next_fork_branch(str(repo), SID) == worktrees.fork_branch_name(SID, 2)


def test_next_fork_branch_uses_the_prefix_and_skips_every_built_in_n(repo: Path) -> None:
    """D-22: a configured prefix names the new fork, and an n an older fork holds under
    ANY built-in prefix (or the configured one) is never reused."""
    _git(repo, "branch", worktrees.fork_branch_name(SID, 1))                    # built-in
    _git(repo, "branch", worktrees.fork_branch_name(SID, 2, brand.LEGACY_BRANCH_PREFIX))
    _git(repo, "branch", worktrees.fork_branch_name(SID, 3, "agent/"))
    assert (worktrees._next_fork_branch(str(repo), SID, "agent/")
            == worktrees.fork_branch_name(SID, 4, "agent/") == "agent/fork-abcd1234-4")


def test_fork_worktree_is_created_under_the_given_prefix(repo: Path, tmp_path: Path) -> None:
    dest = str(tmp_path / "fork-dest")
    info = worktrees.create_fork_worktree(str(repo), source_session_id=SID, dest=dest,
                                          branch_prefix="agent/")
    assert info.branch == "agent/fork-abcd1234-1"


def test_fork_picks_past_an_existing_branch_and_leaves_it(repo: Path, tmp_path: Path) -> None:
    taken = worktrees.fork_branch_name(SID, 1)
    _git(repo, "branch", taken)
    tip = _git(repo, "rev-parse", taken)
    info = _fork(repo, tmp_path / "wt")
    assert info.branch == worktrees.fork_branch_name(SID, 2)
    assert _git(repo, "rev-parse", taken) == tip


@pytest.mark.parametrize("rel,ok", [
    (b"a/b.txt", True), (b"../x", False), (b"/abs", False), (b"a/../b", False),
    (b"nested/", False), (b"a/.git/config", False), (b".GIT/x", False), (b"a//b", False),
    (b"./a", False), (b"", False)])
def test_rel_ok(rel: bytes, ok: bool) -> None:
    assert worktrees._rel_ok(rel) is ok


# -- the fork itself -----------------------------------------------------------------

def test_child_carries_every_kind_of_change_and_source_is_untouched(
        repo: Path, tmp_path: Path) -> None:
    _dirty(repo)
    before = _snapshot(repo)
    dest = tmp_path / "wt"

    info = _fork(repo / "sub", dest)

    assert _snapshot(repo) == before
    assert info.path == str(dest)
    assert info.cwd == str(dest / "sub")          # mirrors the source's subdir
    assert info.branch == worktrees.fork_branch_name(SID, 1)
    assert info.repo == str(repo)
    assert _git(dest, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == info.branch
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


def test_staged_rename_deletion_and_mode_change_carry_over(repo: Path, tmp_path: Path) -> None:
    _git(repo, "mv", "sub/b.txt", "sub/renamed.txt")     # staged rename
    _git(repo, "rm", "-q", "d.txt")                      # staged deletion
    os.chmod(repo / "touched.txt", 0o755)                # tracked mode change (unstaged)
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    _fork(repo, dest)
    assert _snapshot(repo) == before
    staged = _git(dest, "diff", "--cached", "--name-status", "-M").split("\n")
    assert "R100\tsub/b.txt\tsub/renamed.txt" in staged
    assert "D\td.txt" in staged
    assert not (dest / "d.txt").exists()
    assert os.stat(dest / "touched.txt").st_mode & 0o111
    assert _git(dest, "diff", "--name-only").split() == ["touched.txt"]


def test_detached_head_source(repo: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    (repo / "a.txt").write_text("detached edit\n")
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    info = _fork(repo, dest)
    assert _snapshot(repo) == before
    assert _git(dest, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == info.branch
    assert (dest / "a.txt").read_text() == "detached edit\n"


def test_clean_source_forks_to_a_clean_child(repo: Path, tmp_path: Path) -> None:
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    info = _fork(repo, dest)
    assert _snapshot(repo) == before
    assert info.cwd == str(dest)
    assert _git(dest, "status", "--porcelain") == ""


def _snapshot_linked(path: Path) -> tuple[bytes, str, bytes]:
    index = Path(_git(path, "rev-parse", "--path-format=absolute",
                      "--git-path", "index").strip())
    idx = index.read_bytes()
    status = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v2", "-z"],
        check=True, capture_output=True).stdout
    return idx, _git(path, "rev-parse", "HEAD"), status


def test_fork_from_a_session_worktree_source(repo: Path, tmp_path: Path) -> None:
    """The source is normally itself a linked worktree, not the clone."""
    src = tmp_path / "src-wt"
    _git(repo, "worktree", "add", "-q", "-b", "viberpit/src", str(src))
    _dirty(src)
    before = _snapshot_linked(src)
    dest = tmp_path / "wt"
    info = _fork(src, dest)
    assert _snapshot_linked(src) == before
    assert info.repo == str(repo)
    assert (dest / "u.txt").read_text() == "untracked\n"
    assert (dest / "a.txt").read_text() == "a3 staged\n"


def test_symlink_and_fifo_untracked_entries_are_skipped(repo: Path, tmp_path: Path) -> None:
    os.symlink("/etc/passwd", repo / "link")
    os.mkfifo(repo / "pipe")
    (repo / "keep.txt").write_text("keep\n")
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    _fork(repo, dest)
    assert _snapshot(repo) == before
    assert (dest / "keep.txt").read_text() == "keep\n"
    assert not os.path.lexists(dest / "link")
    assert not os.path.lexists(dest / "pipe")


def test_missing_source_index_fails_and_is_not_created(repo: Path, tmp_path: Path) -> None:
    (repo / ".git" / "index").unlink()
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_FAILED"
    assert not (repo / ".git" / "index").exists()
    assert not dest.exists()


# -- repo-config command execution ---------------------------------------------------

def test_repo_local_merge_driver_never_runs(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    _git(repo, "config", "merge.evil.driver", f"touch {marker}; exit 1")
    _git(repo, "config", "merge.ff", "true")             # a plain merge.* setting
    (repo / ".gitattributes").write_text("* merge=evil\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    # Sanity: the driver does fire on a real merge in plain git.
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    _git(repo, "commit", "-qam", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "side"], capture_output=True)
    assert marker.exists()
    marker.unlink()
    _git(repo, "merge", "--abort")

    # The override neutralises it on that same real merge.
    overrides = merge_driver_overrides(str(repo))
    assert "merge.evil.driver=false" in overrides
    subprocess.run(["git", "-C", str(repo), *overrides, "merge", "-q", "side"],
                   capture_output=True)
    assert not marker.exists()
    _git(repo, "merge", "--abort")
    _dirty(repo)
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    _fork(repo, dest)
    assert not marker.exists()
    assert _snapshot(repo) == before
    assert (dest / "a.txt").read_text() == "a3 staged\n"


# -- concurrency ---------------------------------------------------------------------

def _race(repo: Path, dests: list[Path]) -> list[object]:
    barrier = threading.Barrier(len(dests))
    results: list[object] = [None] * len(dests)

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = _fork(repo, dests[i])
        except ForkWorktreeError as exc:
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(dests))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_forks_get_distinct_branches(repo: Path, tmp_path: Path) -> None:
    _dirty(repo)
    results = _race(repo, [tmp_path / "wt1", tmp_path / "wt2"])
    assert all(isinstance(r, worktrees.ForkWorktreeInfo) for r in results)
    infos = [r for r in results if isinstance(r, worktrees.ForkWorktreeInfo)]
    assert {i.branch for i in infos} == {worktrees.fork_branch_name(SID, n) for n in (1, 2)}
    for info in infos:
        assert Path(info.path, "u.txt").read_text() == "untracked\n"
        assert _git(info.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == info.branch


def test_concurrent_forks_to_one_dest_never_harm_the_winner(repo: Path, tmp_path: Path) -> None:
    _dirty(repo)
    dest = tmp_path / "wt"
    results = _race(repo, [dest, dest])
    wins = [r for r in results if isinstance(r, worktrees.ForkWorktreeInfo)]
    losses = [r for r in results if isinstance(r, ForkWorktreeError)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].code == "FORK_DEST_EXISTS"
    assert (dest / "u.txt").read_text() == "untracked\n"
    assert str(dest) in _registered(repo)
    assert wins[0].branch in _branches(repo)


def test_failed_add_discards_nothing(repo: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A racer outside the lock (the agent) took the name: add fails, and the
    branch and its worktree are left exactly as they were."""
    theirs = worktrees.fork_branch_name(SID, 1)
    _git(repo, "branch", theirs)      # not checked out: a discard WOULD delete it
    tip = _git(repo, "rev-parse", theirs)
    monkeypatch.setattr(worktrees, "_next_fork_branch", lambda *_a: theirs)
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_FAILED"
    assert _git(repo, "rev-parse", theirs) == tip


# -- failure cleanup -----------------------------------------------------------------

def _assert_no_half_fork(repo: Path, dest: Path) -> None:
    assert not dest.exists()
    assert not [b for b in _branches(repo) if "/fork-" in b]
    assert str(dest) not in _registered(repo)


def test_untracked_over_the_size_bound_fails_and_cleans_up(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees, "FORK_MAX_UNTRACKED_BYTES", 8)
    (repo / "big.txt").write_text("0123456789\n")
    before = _snapshot(repo)
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_UNTRACKED_TOO_LARGE"
    assert _snapshot(repo) == before
    _assert_no_half_fork(repo, dest)


def test_failure_partway_removes_the_half_made_worktree(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dirty(repo)
    before = _snapshot(repo)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(worktrees, "_copy_untracked", boom)
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_FAILED"
    assert _snapshot(repo) == before
    _assert_no_half_fork(repo, dest)


def test_existing_dest_is_refused_and_left_alone(repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "wt"
    dest.mkdir()
    (dest / "precious").write_text("mine\n")
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_DEST_EXISTS"
    assert (dest / "precious").read_text() == "mine\n"
    assert not [b for b in _branches(repo) if "/fork-" in b]


def test_non_repo_source_raises(tmp_path: Path) -> None:
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(tmp_path, tmp_path / "wt")
    assert ei.value.code == "FORK_NOT_A_REPO"


# -- the copy, directly --------------------------------------------------------------

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


def test_copy_never_reads_through_a_swapped_in_source_symlink(tmp_path: Path) -> None:
    src, dst, outside = tmp_path / "src", tmp_path / "dst", tmp_path / "outside"
    for d in (src, dst, outside):
        d.mkdir()
    (outside / "secret").write_text("secret\n")
    os.symlink(outside, src / "d")          # "d/" was a real dir when planned
    worktrees._copy_untracked(str(src), str(dst), [b"d/secret"])
    assert not os.path.lexists(dst / "d")


def test_copy_never_creates_through_a_child_symlink(tmp_path: Path) -> None:
    """A tracked symlink in the child pointing outside cannot carry the copy out."""
    src, dst, outside = tmp_path / "src", tmp_path / "dst", tmp_path / "outside"
    for d in (src, dst, outside):
        d.mkdir()
    (src / "out" / "deeper").mkdir(parents=True)
    (src / "out" / "deeper" / "x").write_text("x\n")
    os.symlink(outside, dst / "out")
    with pytest.raises(ForkWorktreeError):
        worktrees._copy_untracked(str(src), str(dst), [b"out/deeper/x"])
    assert list(outside.iterdir()) == []


# -- fix round 2 ---------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["onbranch", "gitdir"])
def test_conditional_include_refuses_the_fork(repo: Path, tmp_path: Path, mode: str,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """An includeIf that only matches the CHILD's context (fork branch / new gitdir)
    could add a filter the source-context scan never sees: refuse outright."""
    marker = tmp_path / "pwned"
    evil = tmp_path / "evil.cfg"
    evil.write_text(f'[filter "x"]\n\tsmudge = touch {marker} && cat\n')
    (repo / ".gitattributes").write_text("*.txt filter=x\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    cond = (f"onbranch:{brand.BRANCH_PREFIX}fork-**" if mode == "onbranch"
            else f"gitdir:{repo}/.git/worktrees/**")
    with open(repo / ".git" / "config", "a") as fh:
        fh.write(f'[includeIf "{cond}"]\n\tpath = {evil}\n')
    (repo / "a.txt").write_text("changed\n")
    before = _snapshot(repo)

    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_UNSAFE_CONFIG"
    assert not marker.exists()
    assert _snapshot(repo) == before
    _assert_no_half_fork(repo, dest)

    # Sanity: without the guard the probe really does fire in the child's context.
    monkeypatch.setattr(worktrees, "untrusted_conditional_includes", lambda _top: [])
    _fork(repo, tmp_path / "wt2")
    assert marker.exists()


def test_non_ascii_digit_suffix_does_not_break_naming(repo: Path) -> None:
    _git(repo, "branch", worktrees.fork_branch_name(SID, 1)[:-1] + "²")   # "…-²"
    assert worktrees._next_fork_branch(str(repo), SID) == worktrees.fork_branch_name(SID, 1)


def test_timeout_during_add_discards_what_it_made(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = worktrees.run_git

    def killed_mid_add(cwd: str, *args: str, **kw: object) -> bytes | None:
        out = real(cwd, *args, **kw)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args:
            raise worktrees.GitTimeout
        return out

    monkeypatch.setattr(worktrees, "run_git", killed_mid_add)
    dest = tmp_path / "wt"
    with pytest.raises(ForkWorktreeError) as ei:
        _fork(repo, dest)
    assert ei.value.code == "FORK_FAILED"
    _assert_no_half_fork(repo, dest)


# -- fix round 3: per-worktree config (extensions.worktreeConfig) ---------------------

@pytest.mark.parametrize("where", ["main", "source"])
def test_worktree_scope_filter_never_reaches_the_child(repo: Path, tmp_path: Path,
                                                       where: str) -> None:
    """git copies the CURRENT worktree's config.worktree into a new worktree. The fork
    adds from the source, whose worktree scope is the one scanned and neutralised;
    a filter in the main worktree's (unscanned) config.worktree must not run."""
    (repo / ".gitattributes").write_text("*.txt filter=x\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    src = tmp_path / "src-wt"
    _git(repo, "worktree", "add", "-q", "-b", "viberpit/src", str(src))
    _git(repo, "config", "extensions.worktreeConfig", "true")
    marker = tmp_path / "pwned"
    _git(repo if where == "main" else src, "config", "--worktree",
         "filter.x.smudge", f"touch {marker} && cat")
    (src / "a.txt").write_text("changed\n")

    # Sanity: a plain add from that worktree does run the driver.
    _git(repo if where == "main" else src, "worktree", "add", "-q", "--detach",
         str(tmp_path / "plain"))
    assert marker.exists()
    marker.unlink()

    dest = tmp_path / "wt"
    info = _fork(src, dest)
    assert not marker.exists()
    assert (dest / "a.txt").read_text() == "changed\n"
    assert info.branch in _branches(repo)
