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
    # Stat-dirty, content-clean: porcelain ``git diff`` rewrites the index for such a
    # file even under --no-optional-locks. A plain ``os.utime()`` can land on the
    # recorded mtime (coarse fs timestamps), so the guard only fired sometimes; an
    # explicit future mtime makes it stat-dirty on every run.
    future = (repo / "ws.txt").stat().st_mtime_ns + 5_000_000_000
    os.utime(repo / "ws.txt", ns=(future, future))
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


def _spy_fetches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], int, str]]:
    """Record (pathspecs, bytes returned, status) for every real patch fetch."""
    calls: list[tuple[list[str], int, str]] = []
    real = worktree_diff._git_bounded

    def spy(cwd: str, args: list[str], limit: int, block_cap: int) -> tuple[bytes, str]:
        data, status = real(cwd, args, limit, block_cap)
        calls.append((args[args.index("--") + 1:], len(data), status))
        return data, status

    monkeypatch.setattr(worktree_diff, "_git_bounded", spy)
    return calls


def test_single_huge_line_is_cut_at_file_cap_and_next_file_refetched(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "bundle.min.js").write_text("a" * (4 << 20) + "\n")
    (repo / "zz.txt").write_text("z\n")
    _commit(repo, "bundle")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    (repo / "bundle.min.js").write_text("b" * (4 << 20) + "\n")
    (repo / "zz.txt").write_text("zz\n")
    calls = _spy_fetches(monkeypatch)
    d = session_diff(str(repo))
    files = _by_path(d)
    assert files["bundle.min.js"]["tooLarge"] is True
    assert files["bundle.min.js"]["hunks"] == []
    assert (files["bundle.min.js"]["additions"], files["bundle.min.js"]["deletions"]) == (1, 1)
    # The ~8 MiB patch was killed one read chunk past the per-file cap ...
    assert calls[0][2] == "cut"
    assert MAX_FILE_PATCH_BYTES < calls[0][1] <= MAX_FILE_PATCH_BYTES + 65536
    # ... and the next file was fetched afresh and still lands.
    assert calls[1][0] == ["zz.txt"] and calls[1][2] == "ok"
    assert len(files["zz.txt"]["hunks"]) == 1
    assert d["truncated"] is False
    assert sum(n for _, n, _ in calls) <= worktree_diff.MAX_TOTAL_BYTES


def test_total_patch_read_is_capped_across_fetches(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktree_diff, "MAX_TOTAL_BYTES", 3000)
    for i in range(10):
        (repo / f"t{i}.txt").write_text("".join(f"line {j}\n" for j in range(60)))
    _commit(repo, "ten")
    calls = _spy_fetches(monkeypatch)
    d = session_diff(str(repo))
    assert d["truncated"] is True
    assert len(calls) == 1 and calls[0][2] == "cut"
    assert calls[0][1] <= 3001
    with_hunks = [f for f in d["files"] if f["hunks"]]
    assert 0 < len(with_hunks) < 10
    assert d["additions"] == 600  # numstat totals are unaffected


def test_failed_fetch_truncates_once_without_refetch_loop(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        (repo / f"f{i}.txt").write_text("x\n")
    calls: list[list[str]] = []

    def dead(cwd: str, args: list[str], limit: int, block_cap: int) -> tuple[bytes, str]:
        calls.append(args)
        return b"", "failed"

    _commit(repo, "five")
    monkeypatch.setattr(worktree_diff, "_git_bounded", dead)
    d = session_diff(str(repo))
    assert len(calls) == 1
    assert d["truncated"] is True
    assert all(f["hunks"] == [] for f in d["files"])
    assert d["additions"] == 5


def test_completed_fetch_missing_a_block_truncates(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    (repo / "ws.txt").write_text("alpha\nbeta\ngamma\n")
    real = worktree_diff._git_bounded

    def drop_keep(cwd: str, args: list[str], limit: int, block_cap: int) -> tuple[bytes, str]:
        data, status = real(cwd, args, limit, block_cap)
        assert status == "ok"
        kept = [b for h, b in worktree_diff._split_patch(data) if b"keep.txt" not in h]
        return b"".join(kept), status

    monkeypatch.setattr(worktree_diff, "_git_bounded", drop_keep)
    d = session_diff(str(repo))
    files = _by_path(d)
    assert files["keep.txt"]["hunks"] == []
    assert d["truncated"] is True  # not a silent "no hunks"
    assert files["keep.txt"]["additions"] == 1  # numstat counts are unaffected


def test_nonzero_git_exit_is_a_failed_fetch(tmp_path: Path) -> None:
    data, status = worktree_diff._git_bounded(str(tmp_path), ["diff", "--no-such-flag"],
                                              1 << 20, MAX_FILE_PATCH_BYTES)
    assert (data, status) == (b"", "failed")


def test_line_count_precheck_skips_fetch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "many.txt").write_text("".join(f"{i}\n" for i in range(MAX_FILE_PATCH_BYTES // 2 + 1)))
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    _commit(repo, "many lines")
    calls = _spy_fetches(monkeypatch)
    files = _by_path(session_diff(str(repo)))
    assert files["many.txt"]["tooLarge"] is True and files["many.txt"]["hunks"] == []
    assert files["many.txt"]["additions"] == MAX_FILE_PATCH_BYTES // 2 + 1
    assert calls and all("many.txt" not in specs for specs, _, _ in calls)
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


def test_repo_textconv_helper_never_runs(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "textconv-ran"
    _git(repo, "config", "diff.foo.textconv", f"touch {marker} && cat")
    (repo / ".gitattributes").write_text("*.txt diff=foo\n")
    _commit(repo, "attrs")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    for summary in (False, True):
        files = _by_path(session_diff(str(repo), summary=summary))
        assert not marker.exists()
        assert files["keep.txt"]["additions"] == 1
    (h,) = _by_path(session_diff(str(repo)))["keep.txt"]["hunks"]  # the raw change
    assert h["lines"][-1] == {"kind": "add", "oldNo": None, "newNo": 21, "text": "tail"}
    assert not marker.exists()
    # The fixture is live: plain git diff WOULD run the helper.
    _git(repo, "diff", "HEAD")
    assert marker.exists()


def test_git_env_disables_system_config_and_hooks(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "keep.txt").write_text("changed\n")
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(args, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append((list(args), kw["env"]))
        return real_run(args, *a, **kw)

    def popen(args, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append((list(args), kw["env"]))
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(worktree_diff.subprocess, "run", run)
    monkeypatch.setattr(worktree_diff.subprocess, "Popen", popen)
    session_diff(str(repo))
    assert any("-U3" in args for args, _ in seen)  # the streamed patch fetch was seen too
    for args, env in seen:
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "core.hooksPath=/dev/null" in args
        if "diff" in args:
            assert "--no-textconv" in args and "--no-ext-diff" in args


def test_internal_key_error_is_not_surfaced_as_key_error(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise KeyError("x")

    monkeypatch.setattr(worktree_diff, "_parse_name_status", boom)
    (repo / "keep.txt").write_text("changed\n")
    with pytest.raises(RuntimeError) as info:
        session_diff(str(repo))
    assert not isinstance(info.value, KeyError)


def _filter_fixture(repo: Path, tmp_path: Path, var: str) -> Path:
    """A repo-local ``filter.x.<var>`` that touches a marker, selected for ``*.txt``,
    with a stat-dirty, content-changed tracked ``.txt``."""
    marker = tmp_path / f"filter-{var}-ran"
    (repo / ".gitattributes").write_text("*.txt filter=x\n")
    _commit(repo, "attrs")  # committed before the filter exists: no marker yet
    _git(repo, "config", f"filter.x.{var}", f"touch {marker}; cat")
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    os.utime(repo / "ws.txt")  # stat-dirty, unchanged content: forces a re-hash
    assert not marker.exists()
    return marker


@pytest.mark.parametrize("var", ["clean", "process"])
def test_repo_local_filter_driver_never_runs(repo: Path, tmp_path: Path, var: str) -> None:
    marker = _filter_fixture(repo, tmp_path, var)
    for summary in (False, True):
        files = _by_path(session_diff(str(repo), summary=summary))
        assert not marker.exists(), summary
        assert files["keep.txt"]["additions"] == 1
    if var == "clean":
        # The fixture is live: plain git diff DOES run a local clean driver.
        _git(repo, "diff", "HEAD")
        assert marker.exists()
    else:
        # A bogus long-running process driver makes plain git fail, but still runs it.
        env = {**os.environ, **_GIT_ENV}
        subprocess.run(["git", "-C", str(repo), "diff", "HEAD"], capture_output=True, env=env)
        assert marker.exists()


def test_worktree_scope_filter_driver_never_runs(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "wt-filter-ran"
    (repo / ".gitattributes").write_text("*.txt filter=w\n")
    _commit(repo, "attrs")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "filter.w.clean", f"touch {marker}; cat")
    assert "filter.w.clean" not in (repo / ".git" / "config").read_text()
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    session_diff(str(repo))
    assert not marker.exists()
    _git(repo, "diff", "HEAD")
    assert marker.exists()


def test_unsafe_filter_driver_name_fails_closed(repo: Path) -> None:
    _git(repo, "config", "filter.a=b.clean", "true")
    (repo / "keep.txt").write_text("changed\n")
    d = session_diff(str(repo))
    assert d["files"] == [] and d["truncated"] is True


def test_global_filter_driver_is_not_overridden(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "global-filter-ran"
    global_cfg = tmp_path / "global.gitconfig"
    global_cfg.write_text(f'[filter "g"]\n\tclean = touch {marker}; cat\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    (repo / ".gitattributes").write_text("*.txt filter=g\n")
    _commit(repo, "attrs")
    _git(repo, "config", "filter.x.clean", "cat")  # a local one, to prove the split
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    marker.unlink(missing_ok=True)
    seen: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(args, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append(list(args))
        return real_run(args, *a, **kw)

    def popen(args, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append(list(args))
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(worktree_diff.subprocess, "run", run)
    monkeypatch.setattr(worktree_diff.subprocess, "Popen", popen)
    session_diff(str(repo))
    diff_calls = [a for a in seen if "diff" in a]
    assert diff_calls
    for args in diff_calls:
        assert not any(a.startswith("filter.g.") for a in args)
        assert "filter.x.clean=" in args and "filter.x.required=false" in args
    assert marker.exists()  # the global driver still ran on the re-hash


def test_filter_driver_from_local_include_never_runs(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "include-filter-ran"
    inc = tmp_path / "inc.gitconfig"
    inc.write_text(f'[filter "inc"]\n\tclean = touch {marker}; cat\n')
    (repo / ".gitattributes").write_text("*.txt filter=inc\n")
    _commit(repo, "attrs")
    _git(repo, "config", "include.path", str(inc))
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    session_diff(str(repo))
    assert not marker.exists()
    _git(repo, "diff", "HEAD")
    assert marker.exists()


def _submodule_fixture(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A committed submodule ``sub`` whose own config (``.git/modules/sub/config``)
    defines a marker-touching ``filter.evil.clean`` selected for every file, with a
    stat-dirty file inside the submodule and a changed superproject file."""
    marker = tmp_path / "submodule-filter-ran"
    subsrc = tmp_path / "subsrc"
    subsrc.mkdir()
    _git(subsrc, "init", "-q", "-b", "main")
    (subsrc / "inner.txt").write_text("inner\n")
    _commit(subsrc, "inner")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(subsrc), "sub")
    _commit(repo, "add sub")
    sub = repo / "sub"
    (sub / ".gitattributes").write_text("* filter=evil\n")
    _commit(sub, "attrs")  # before the filter exists: no marker yet
    _git(repo, "add", "sub")
    _commit(repo, "bump sub")
    modcfg = repo / ".git" / "modules" / "sub" / "config"
    assert modcfg.exists()
    _git(sub, "config", "filter.evil.clean", f"touch {marker}; cat")
    os.utime(sub / "inner.txt")  # stat-dirty, unchanged content: forces a re-hash
    (repo / "keep.txt").write_text((repo / "keep.txt").read_text() + "tail\n")
    assert not marker.exists()
    return marker, sub


def test_submodule_config_filter_driver_never_runs(repo: Path, tmp_path: Path) -> None:
    marker, _ = _submodule_fixture(repo, tmp_path)
    for summary in (False, True):
        files = _by_path(session_diff(str(repo), summary=summary))
        assert not marker.exists(), summary
        assert files["keep.txt"]["additions"] == 1
    # The fixture is live: plain git diff DOES run the submodule's clean driver.
    _git(repo, "diff", "HEAD")
    assert marker.exists()


def test_submodule_pointer_change_is_still_reported(repo: Path, tmp_path: Path) -> None:
    _, sub = _submodule_fixture(repo, tmp_path)
    _git(repo, "push", "-q", "origin", "HEAD:main")  # the submodule is in the base
    _git(repo, "fetch", "-q", "origin")
    (sub / "more.txt").write_text("more\n")
    _git(sub, "-c", "filter.evil.clean=cat", "add", "-A")
    _git(sub, "commit", "-q", "-m", "more")
    files = _by_path(session_diff(str(repo)))
    assert files["sub"]["status"] == "modified"
    assert (files["sub"]["additions"], files["sub"]["deletions"]) == (1, 1)


def test_fifo_swapped_in_after_lstat_never_blocks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    regular = tmp_path / "regular.txt"
    regular.write_text("hello\n")
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    real_lstat, real_stat = os.lstat, os.stat

    def swap(real):  # type: ignore[no-untyped-def]
        def by_path(p, *a, **kw):  # type: ignore[no-untyped-def]
            # Path checks saw a regular file; by the open, a FIFO sits at the path.
            return real(regular if os.fsdecode(p) == str(fifo) else p, *a, **kw)
        return by_path

    monkeypatch.setattr(worktree_diff.os, "lstat", swap(real_lstat))
    monkeypatch.setattr(worktree_diff.os, "stat", swap(real_stat))
    result: list[object] = []
    t = threading.Thread(target=lambda: result.append(worktree_diff._untracked(
        str(tmp_path), b"pipe.txt", want_hunks=True, read_left=1 << 20)), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():  # unblock the stuck open before failing
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        t.join(5)
        pytest.fail("_untracked blocked opening a FIFO")
    got = result[0]
    assert got is not None
    f, size, consumed = got  # type: ignore[misc]
    assert (f["path"], f["additions"], f["hunks"], size, consumed) == ("pipe.txt", 0, [], 0, 0)
