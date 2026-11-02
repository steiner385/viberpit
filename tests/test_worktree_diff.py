"""`worktree_diff.session_diff` — the structured base-to-working-tree diff behind the
Changes pane (D-4). Every case runs real git against a temp clone of a local bare
"origin" whose ``origin/HEAD`` is recorded, so the merge-base rule is exercised for real."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from viberpit import worktree_diff
from viberpit.worktree_diff import MAX_FILES, MAX_FILE_PATCH_BYTES, session_diff

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path | str, *args: str) -> str:
    env = {**os.environ, **_GIT_ENV, "HOME": str(cwd)}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _commit(cwd: Path, msg: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone of a bare origin (default branch ``main``, recorded ``origin/HEAD``),
    checked out on a feature branch at the default's tip."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "keep.txt").write_text("".join(f"line {i}\n" for i in range(1, 21)))
    (seed / "gone.txt").write_text("bye\n")
    (seed / "old_name.txt").write_text("".join(f"row {i}\n" for i in range(1, 11)))
    (seed / "ws.txt").write_text("alpha\nbeta\n")
    _commit(seed, "seed")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD")  # recorded by clone
    _git(work, "checkout", "-q", "-b", "feature")
    return work


def _by_path(d: dict) -> dict[str, dict]:
    return {f["path"]: f for f in d["files"]}


def test_modified_file_hunk_line_numbers(repo: Path) -> None:
    lines = (repo / "keep.txt").read_text().splitlines(keepends=True)
    lines[9] = "line 10 changed\n"
    (repo / "keep.txt").write_text("".join(lines))
    d = session_diff(str(repo))
    f = _by_path(d)["keep.txt"]
    assert f["status"] == "modified"
    assert (f["additions"], f["deletions"]) == (1, 1)
    assert f["oldPath"] is None and f["binary"] is False and f["tooLarge"] is False
    assert len(f["hunks"]) == 1
    h = f["hunks"][0]
    assert (h["oldStart"], h["oldLines"], h["newStart"], h["newLines"]) == (7, 7, 7, 7)
    kinds = [(ln["kind"], ln["oldNo"], ln["newNo"], ln["text"]) for ln in h["lines"]]
    assert kinds[0] == ("context", 7, 7, "line 7")
    assert ("del", 10, None, "line 10") in kinds
    assert ("add", None, 10, "line 10 changed") in kinds
    assert kinds[-1] == ("context", 13, 13, "line 13")
    assert (d["additions"], d["deletions"]) == (1, 1)
    assert d["base"] == _git(repo, "rev-parse", "origin/main").strip()
    assert d["head"] == _git(repo, "rev-parse", "HEAD").strip()
    assert d["truncated"] is False


def test_untracked_file_is_added_from_dev_null(repo: Path) -> None:
    (repo / "new.py").write_text("print(1)\nprint(2)\n")
    f = _by_path(session_diff(str(repo)))["new.py"]
    assert f["status"] == "added"
    assert (f["additions"], f["deletions"]) == (2, 0)
    (h,) = f["hunks"]
    assert (h["oldStart"], h["oldLines"], h["newStart"], h["newLines"]) == (0, 0, 1, 2)
    assert [(ln["kind"], ln["oldNo"], ln["newNo"], ln["text"]) for ln in h["lines"]] == [
        ("add", None, 1, "print(1)"), ("add", None, 2, "print(2)"),
    ]


def test_ignored_untracked_file_is_not_listed(repo: Path) -> None:
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "x.log").write_text("noise\n")
    paths = _by_path(session_diff(str(repo)))
    assert "x.log" not in paths and ".gitignore" in paths


def test_deleted_file(repo: Path) -> None:
    (repo / "gone.txt").unlink()
    f = _by_path(session_diff(str(repo)))["gone.txt"]
    assert f["status"] == "deleted"
    assert (f["additions"], f["deletions"]) == (0, 1)
    assert f["hunks"][0]["lines"] == [{"kind": "del", "oldNo": 1, "newNo": None, "text": "bye"}]


def test_rename_with_edit(repo: Path) -> None:
    _git(repo, "mv", "old_name.txt", "new_name.txt")
    text = (repo / "new_name.txt").read_text().replace("row 5\n", "row five\n")
    (repo / "new_name.txt").write_text(text)
    _commit(repo, "rename")
    files = _by_path(session_diff(str(repo)))
    assert "old_name.txt" not in files
    f = files["new_name.txt"]
    assert f["status"] == "renamed" and f["oldPath"] == "old_name.txt"
    assert (f["additions"], f["deletions"]) == (1, 1)
    assert len(f["hunks"]) == 1


def test_binary_file(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / "tracked.bin").write_bytes(b"\x00abc")
    _commit(repo, "bins")
    (repo / "tracked.bin").write_bytes(b"\x00abcd")
    (repo / "untracked.bin").write_bytes(b"PNG\x00\x00data")
    files = _by_path(session_diff(str(repo)))
    for p in ("blob.bin", "tracked.bin", "untracked.bin"):
        assert files[p]["binary"] is True, p
        assert files[p]["hunks"] == [], p
        assert (files[p]["additions"], files[p]["deletions"]) == (0, 0), p


def test_committed_and_uncommitted_changes_both_appear(repo: Path) -> None:
    (repo / "committed.txt").write_text("c\n")
    _commit(repo, "ahead")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    files = _by_path(session_diff(str(repo)))
    assert files["committed.txt"]["status"] == "added"
    assert files["keep.txt"]["additions"] == 1


def test_default_branch_moving_does_not_leak_into_diff(repo: Path, tmp_path: Path) -> None:
    (repo / "mine.txt").write_text("mine\n")
    _commit(repo, "mine")
    # Someone else lands on origin's default after our branch point.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    (other / "theirs.txt").write_text("theirs\n")
    _commit(other, "theirs")
    _git(other, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    d = session_diff(str(repo))
    assert set(_by_path(d)) == {"mine.txt"}
    assert d["base"] == _git(repo, "merge-base", "HEAD", "origin/main").strip()


def test_no_origin_head_diffs_against_head(repo: Path) -> None:
    (repo / "committed.txt").write_text("c\n")
    _commit(repo, "ahead")
    _git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    d = session_diff(str(repo))
    assert d["base"] is None
    assert set(_by_path(d)) == {"keep.txt"}


def test_not_a_git_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    d = session_diff(str(plain))
    assert d == {"base": None, "head": None, "files": [], "additions": 0, "deletions": 0,
                 "truncated": False}
    assert session_diff(str(tmp_path / "missing"))["files"] == []


def test_subdirectory_cwd_reports_repo_relative_paths(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n")
    (repo / "gone.txt").unlink()
    paths = set(_by_path(session_diff(str(repo / "pkg"))))
    assert paths == {"pkg/mod.py", "gone.txt"}


def test_space_and_unicode_paths_round_trip(repo: Path) -> None:
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "a b.txt").write_text("one\n")
    (repo / "naïve-日本.txt").write_text("two\n")
    (repo / 'quote"tab\there.txt').write_text("three\n")
    _commit(repo, "odd names")
    _git(repo, "push", "-q", "origin", "HEAD:main")  # land them in the base
    _git(repo, "fetch", "-q", "origin")
    (repo / "dir with space" / "a b.txt").write_text("one!\n")
    (repo / "naïve-日本.txt").write_text("two!\n")
    (repo / 'quote"tab\there.txt').write_text("three!\n")
    (repo / "untracked ünï.txt").write_text("u\n")
    files = _by_path(session_diff(str(repo)))
    for p in ("dir with space/a b.txt", "naïve-日本.txt", 'quote"tab\there.txt'):
        assert files[p]["status"] == "modified", p
        assert len(files[p]["hunks"]) == 1, p  # the patch header mapped back to this file
    assert files["untracked ünï.txt"]["status"] == "added"


def test_file_count_cap_truncates(repo: Path) -> None:
    for i in range(MAX_FILES + 5):
        (repo / f"f{i:04d}.txt").write_text("x\n")
    _commit(repo, "many")
    (repo / "zz-untracked.txt").write_text("y\n")
    d = session_diff(str(repo))
    assert d["truncated"] is True
    assert len(d["files"]) == MAX_FILES + 6  # the rest are listed, just without hunks
    assert sum(1 for f in d["files"] if f["hunks"]) == MAX_FILES
    # Tracked totals come from numstat, so they cover every tracked file; an untracked
    # file beyond the cap is listed unread (a lower bound).
    assert d["additions"] == MAX_FILES + 5
    assert _by_path(d)["zz-untracked.txt"]["additions"] == 0


def test_oversized_file_is_too_large(repo: Path) -> None:
    big = "".join(f"{i:08d} {'x' * 70}\n" for i in range(MAX_FILE_PATCH_BYTES // 60))
    (repo / "big.txt").write_text(big)
    _commit(repo, "big")
    (repo / "big_untracked.txt").write_text(big)
    files = _by_path(session_diff(str(repo)))
    n = MAX_FILE_PATCH_BYTES // 60
    for p in ("big.txt", "big_untracked.txt"):
        assert files[p]["tooLarge"] is True, p
        assert files[p]["hunks"] == [], p
        assert files[p]["additions"] == n, p


def test_total_budget_stops_hunks(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktree_diff, "MAX_TOTAL_BYTES", 300)
    for i in range(10):
        (repo / f"t{i}.txt").write_text("".join(f"l{j}\n" for j in range(10)))
    _commit(repo, "ten")
    d = session_diff(str(repo))
    assert d["truncated"] is True
    with_hunks = [f for f in d["files"] if f["hunks"]]
    assert 0 < len(with_hunks) < 10
    assert d["additions"] == 100


def test_summary_has_no_hunks_but_same_totals(repo: Path) -> None:
    (repo / "keep.txt").write_text("changed\n")
    (repo / "new.txt").write_text("a\nb\n")
    full = session_diff(str(repo))
    summ = session_diff(str(repo), summary=True)
    assert all(f["hunks"] == [] for f in summ["files"])
    assert (summ["additions"], summ["deletions"]) == (full["additions"], full["deletions"])
    assert [f["path"] for f in summ["files"]] == [f["path"] for f in full["files"]]


def test_ignore_whitespace_drops_whitespace_only_change(repo: Path) -> None:
    (repo / "ws.txt").write_text("alpha  \n  beta\n")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    assert "ws.txt" in _by_path(session_diff(str(repo)))
    d = session_diff(str(repo), ignore_whitespace=True)
    assert set(_by_path(d)) == {"keep.txt"}
    assert (d["additions"], d["deletions"]) == (1, 0)


def test_no_trailing_newline_marker_is_not_a_line(repo: Path) -> None:
    (repo / "ws.txt").write_text("alpha\nbeta")
    (h,) = _by_path(session_diff(str(repo)))["ws.txt"]["hunks"]
    assert all(not ln["text"].startswith("\\") for ln in h["lines"])
    assert [ln["kind"] for ln in h["lines"]] == ["context", "del", "add"]


def test_read_only_index_untouched(repo: Path) -> None:
    index = repo / ".git" / "index"
    (repo / "keep.txt").write_text("changed\n")
    (repo / "new.txt").write_text("n\n")
    os.utime(repo / "ws.txt")  # stat-dirty: a refreshing git would rewrite the index
    before = index.stat().st_mtime_ns
    session_diff(str(repo))
    session_diff(str(repo), summary=True, ignore_whitespace=True)
    assert index.stat().st_mtime_ns == before
    assert _git(repo, "status", "--porcelain").count("??") == 1  # new.txt never staged


def test_suppress_blank_empty_config_keeps_numbering(repo: Path) -> None:
    _git(repo, "config", "diff.suppressBlankEmpty", "true")
    (repo / "blank.txt").write_text("a\n\nb\nc\n")
    _commit(repo, "blank")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    (repo / "blank.txt").write_text("a\n\nb\nC\n")
    (h,) = _by_path(session_diff(str(repo)))["blank.txt"]["hunks"]
    assert [(ln["kind"], ln["oldNo"], ln["newNo"], ln["text"]) for ln in h["lines"]] == [
        ("context", 1, 1, "a"), ("context", 2, 2, ""), ("context", 3, 3, "b"),
        ("del", 4, None, "c"), ("add", None, 4, "C"),
    ]


def test_parser_treats_bare_empty_line_as_context() -> None:
    block = b"diff --git a/f b/f\n@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n"
    (h,) = worktree_diff._parse_hunks(block)
    assert [(ln["kind"], ln["oldNo"], ln["newNo"]) for ln in h["lines"]] == [
        ("context", 1, 1), ("context", 2, 2), ("del", 3, None), ("add", None, 3),
    ]


def test_custom_diff_prefix_config_still_maps_hunks(repo: Path) -> None:
    _git(repo, "config", "diff.srcPrefix", "x/")
    _git(repo, "config", "diff.dstPrefix", "y/")
    _git(repo, "config", "diff.noprefix", "true")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    assert len(_by_path(session_diff(str(repo)))["keep.txt"]["hunks"]) == 1


def test_single_huge_line_is_too_large_with_bounded_read(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "bundle.min.js").write_text("a" * (1 << 20) + "\n")
    (repo / "zz.txt").write_text("z\n")
    _commit(repo, "bundle")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    (repo / "bundle.min.js").write_text("b" * (1 << 20) + "\n")
    (repo / "zz.txt").write_text("zz\n")
    seen: list[int] = []
    real = worktree_diff._git_bounded

    def spy(cwd: str, args: list[str], limit: int) -> tuple[bytes, bool]:
        data, complete = real(cwd, args, limit)
        seen.append(len(data))
        return data, complete

    monkeypatch.setattr(worktree_diff, "_git_bounded", spy)
    files = _by_path(session_diff(str(repo)))
    assert files["bundle.min.js"]["tooLarge"] is True
    assert files["bundle.min.js"]["hunks"] == []
    assert (files["bundle.min.js"]["additions"], files["bundle.min.js"]["deletions"]) == (1, 1)
    assert len(files["zz.txt"]["hunks"]) == 1  # a later file in the cut batch still lands
    limit = worktree_diff.MAX_TOTAL_BYTES + MAX_FILE_PATCH_BYTES
    assert seen and max(seen) <= limit + 65536  # one read chunk past the limit at most


def test_line_count_precheck_skips_fetch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "many.txt").write_text("".join(f"{i}\n" for i in range(MAX_FILE_PATCH_BYTES // 2 + 1)))
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    _commit(repo, "many lines")
    specs: list[list[str]] = []
    real = worktree_diff._git_bounded

    def spy(cwd: str, args: list[str], limit: int) -> tuple[bytes, bool]:
        specs.append(args[args.index("--") + 1:])
        return real(cwd, args, limit)

    monkeypatch.setattr(worktree_diff, "_git_bounded", spy)
    files = _by_path(session_diff(str(repo)))
    assert files["many.txt"]["tooLarge"] is True and files["many.txt"]["hunks"] == []
    assert files["many.txt"]["additions"] == MAX_FILE_PATCH_BYTES // 2 + 1
    assert all("many.txt" not in s for s in specs)
    assert len(files["keep.txt"]["hunks"]) == 1


def test_untracked_read_budget_stops_reading(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktree_diff, "MAX_TOTAL_BYTES", 1000)
    for i in range(10):
        (repo / f"u{i}.txt").write_text("x" * 99 + "\n" + "y" * 199 + "\n")  # 300 bytes
    for summary in (False, True):
        d = session_diff(str(repo), summary=summary)
        files = _by_path(d)
        assert d["truncated"] is True
        assert [files[f"u{i}.txt"]["additions"] for i in range(10)] == [2, 2, 2] + [0] * 7
        assert d["additions"] == 6


def test_numstat_timeout_is_not_a_clean_tree(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "keep.txt").write_text("changed\n")
    real = subprocess.run

    def slow(args, *a, **kw):  # type: ignore[no-untyped-def]
        if "--numstat" in args:
            raise subprocess.TimeoutExpired(args, 30)
        return real(args, *a, **kw)

    monkeypatch.setattr(worktree_diff.subprocess, "run", slow)
    d = session_diff(str(repo))
    assert d["files"] == [] and d["truncated"] is True
    assert d["head"] == _git(repo, "rev-parse", "HEAD").strip()
