"""D-7 files layer: hardened worktree file list + read, against real temp git repos."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server.permission_rules.patterns import worktree_rel, worktree_rel_path
from server.sessions import worktree_files
from server.sessions.worktree_files import (
    MAX_LIST_LIMIT,
    MAX_READ_BYTES,
    MAX_SCAN_ENTRIES,
    FileAccessError,
    list_files,
    read_file,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / ".gitignore").write_text("ignored.log\nbuild/\n")
    (r / "tracked.txt").write_text("hello\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _paths(res: dict) -> list[str]:
    return [f["path"] for f in res["files"]]


def _code(cwd: Path | str, path: str) -> str:
    with pytest.raises(FileAccessError) as ei:
        read_file(str(cwd), path)
    return ei.value.code


# ------------------------------------------------------------------ listing


def test_lists_tracked_and_untracked_but_not_ignored(repo: Path) -> None:
    (repo / "new.py").write_text("x = 1\n")
    (repo / "ignored.log").write_text("noise\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("x\n")
    res = list_files(str(repo), limit=200)
    paths = set(_paths(res))
    assert {"tracked.txt", "new.py", ".gitignore"} <= paths
    assert "ignored.log" not in paths and "build/out.js" not in paths
    assert res["truncated"] is False


def test_paths_are_cwd_relative_and_slash_separated_from_a_subdir_cwd(repo: Path) -> None:
    """The CLI resolves ``@path`` against the session cwd, so a subdirectory session
    lists exactly the files beneath it, relative to it — nothing above it."""
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / "sub" / "mod.py").write_text("x\n")
    (repo / "pkg" / "top.py").write_text("x\n")
    res = list_files(str(repo / "pkg"), q="mod", limit=10)
    assert _paths(res) == ["sub/mod.py"]
    assert sorted(_paths(list_files(str(repo / "pkg"), limit=200))) == ["sub/mod.py", "top.py"]
    # The empty-query mtime sort stats cwd-relative paths too (nothing dropped).
    assert len(_paths(list_files(str(repo / "pkg"), limit=200))) == 2


def test_fuzzy_basename_match_beats_scattered_directory_match(repo: Path) -> None:
    for p in ("src/Composer.tsx", "src/a/b/c/o/m/p.ts", "README.md"):
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("x\n")
    res = list_files(str(repo), q="comp")
    paths = _paths(res)
    assert paths.index("src/Composer.tsx") < paths.index("src/a/b/c/o/m/p.ts")
    assert "README.md" not in paths  # not a subsequence


def test_fuzzy_basename_beats_directory_and_contiguous_beats_scattered(repo: Path) -> None:
    for p in ("comp/zzz.ts", "lib/comp.ts", "lib/c_o_m_p.ts"):
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("x\n")
    paths = _paths(list_files(str(repo), q="COMP"))  # case-insensitive
    assert paths.index("lib/comp.ts") < paths.index("comp/zzz.ts")
    assert paths.index("lib/comp.ts") < paths.index("lib/c_o_m_p.ts")


def test_fuzzy_tiers_prefix_then_word_boundary_then_infix_then_directory(repo: Path) -> None:
    """``comp``: a basename prefix beats a word-boundary match (``_compat``), which beats
    an infix, which beats a directory match, which beats a scattered one."""
    for p in ("web/src/features/sessions/composer/Composer.tsx", "src/viberpit/env_compat.py",
              "server/sessions/completion.py", "a/zcompx.py", "comp/z.py", "c/o/m/p.py",
              "src/useCompose.ts"):
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("x\n")
    paths = _paths(list_files(str(repo), q="comp"))
    assert paths == ["web/src/features/sessions/composer/Composer.tsx",
                     "server/sessions/completion.py",  # prefix too, longer stem
                     "src/useCompose.ts",              # camelCase hump
                     "src/viberpit/env_compat.py",     # after "_"
                     "a/zcompx.py", "comp/z.py", "c/o/m/p.py"]


def test_fuzzy_shorter_path_wins_ties(repo: Path) -> None:
    for p in ("deep/nested/dir/util.ts", "a/util.ts"):
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("x\n")
    paths = _paths(list_files(str(repo), q="util"))
    assert paths[:2] == ["a/util.ts", "deep/nested/dir/util.ts"]


def test_empty_query_lists_most_recently_modified_first(repo: Path) -> None:
    now = time.time()
    for i, name in enumerate(("old.txt", "mid.txt", "new.txt")):
        (repo / name).write_text("x\n")
        os.utime(repo / name, (now - 1000 + i * 100, now - 1000 + i * 100))
    os.utime(repo / "tracked.txt", (now - 5000, now - 5000))
    os.utime(repo / ".gitignore", (now - 5000, now - 5000))
    assert _paths(list_files(str(repo), limit=3)) == ["new.txt", "mid.txt", "old.txt"]


def test_limit_defaults_to_50_and_caps_at_200(repo: Path) -> None:
    for i in range(260):
        (repo / f"f{i:03}.txt").write_text("x\n")
    assert len(list_files(str(repo))["files"]) == 50
    assert MAX_LIST_LIMIT == 200
    assert len(list_files(str(repo), limit=10_000)["files"]) == 200
    assert len(list_files(str(repo), q="f", limit=10_000)["files"]) == 200


def test_scan_cap_sets_truncated(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_SCAN_ENTRIES == 20_000
    for i in range(12):
        (repo / f"f{i:02}.txt").write_text("x\n")
    monkeypatch.setattr(worktree_files, "MAX_SCAN_ENTRIES", 5)
    res = list_files(str(repo), limit=200)
    assert res["truncated"] is True
    assert len(res["files"]) == 5
    monkeypatch.setattr(worktree_files, "MAX_SCAN_ENTRIES", 14)  # 12 + tracked + .gitignore
    res = list_files(str(repo), limit=200)
    assert res["truncated"] is False and len(res["files"]) == 14


def test_non_utf8_and_nested_repo_entries_are_skipped(repo: Path) -> None:
    with open(os.path.join(os.fsencode(repo), b"bad\xff.txt"), "wb") as fh:
        fh.write(b"x\n")
    (repo / "nested").mkdir()
    _git(repo / "nested", "init", "-q")
    (repo / "nested" / "inner.txt").write_text("x\n")
    res = list_files(str(repo), limit=200)
    assert set(_paths(res)) == {".gitignore", "tracked.txt"}
    import json
    json.dumps(res)


def test_no_cwd_or_non_git_cwd_lists_nothing(tmp_path: Path) -> None:
    assert list_files("") == {"files": [], "truncated": False}
    assert list_files(str(tmp_path / "missing")) == {"files": [], "truncated": False}
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "a.txt").write_text("x\n")
    assert list_files(str(tmp_path / "plain")) == {"files": [], "truncated": False}


def test_listing_ignores_repo_redirect_env(repo: Path, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "elsewhere.txt").write_text("x\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    paths = _paths(list_files(str(repo), limit=200))
    assert "tracked.txt" in paths and "elsewhere.txt" not in paths


def _crafted(n: int) -> str:
    """A path the old ranker spent ~``len(path) * len(q)`` on per start, for every start."""
    return "d/" + ("a" + "x" * 30) * (n // 31) + ".txt"


class _CountingStr(str):
    """A str whose ``find`` calls are counted — the ranker's unit of work."""
    calls = 0

    def find(self, *a, **kw):  # type: ignore[no-untyped-def,override]
        _CountingStr.calls += 1
        return super().find(*a, **kw)


def test_ranking_work_per_path_is_bounded() -> None:
    """A crafted 4 KiB path and a 64-char query. Unbounded, every one of ~130 starts
    walked the whole query (≈8k finds a path; 20k such paths took minutes). Now at most
    ``MAX_MATCH_STARTS`` starts are tried."""
    q = "a" * 64
    _CountingStr.calls = 0
    assert worktree_files._runs(q, _CountingStr(_crafted(4096))) is not None
    assert worktree_files.MAX_MATCH_STARTS == 16
    assert _CountingStr.calls <= 16 * len(q) + 1


def test_long_path_is_ranked_on_its_tail_only() -> None:
    """Over 1 KiB, only the last 512 chars are matched: a match buried at the front of a
    crafted path does not cost (or score) anything."""
    head = "needle/" + "x" * 2000 + "/"
    assert worktree_files._rank_key("needle", head + "f.txt") is None
    assert worktree_files._rank_key("needle", "needle/" + "f.txt") is not None
    tail = "x" * 2000 + "/needle.txt"
    key = worktree_files._rank_key("needle", tail)
    assert key is not None and key[0] == 0 and key[-1] == tail  # the full path comes back


def test_query_is_cut_to_64_chars(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = worktree_files._rank_key

    def spy(q: str, p: str):  # type: ignore[no-untyped-def]
        seen.append(q)
        return real(q, p)

    monkeypatch.setattr(worktree_files, "_rank_key", spy)
    list_files(str(repo), q="t" * 500)
    assert seen and {len(q) for q in seen} == {worktree_files.MAX_QUERY_CHARS} == {64}


def test_ranking_deadline_returns_best_so_far_truncated(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past ``RANK_DEADLINE_S`` the pass stops: what it ranked so far comes back, best
    first, with ``truncated``. The clock jumps 0.1 s per read, so two paths fit."""
    for i in range(20):
        (repo / f"f{i:02}.txt").write_text("x\n")
    ticks = iter(range(10_000))
    monkeypatch.setattr(worktree_files, "_clock", lambda: next(ticks) * 0.1)
    res = list_files(str(repo), q="f", limit=50)
    assert res["truncated"] is True
    assert len(res["files"]) == 2
    assert worktree_files.RANK_DEADLINE_S == 0.25


def test_ranking_within_deadline_is_not_truncated(repo: Path) -> None:
    assert list_files(str(repo), q="tracked")["truncated"] is False


# ------------------------------------------------------------------ reading


def test_reads_text(repo: Path) -> None:
    res = read_file(str(repo), "tracked.txt")
    assert res == {"path": "tracked.txt", "text": "hello\n", "binary": False,
                   "truncated": False, "size": 6}


def test_reads_dotfiles_and_cwd_relative_from_subdir_cwd(repo: Path) -> None:
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / ".envrc").write_text("export A=1\n")
    (repo / "pkg" / "sub" / "m.py").write_text("m\n")
    assert read_file(str(repo / "pkg"), ".envrc")["text"] == "export A=1\n"
    res = read_file(str(repo / "pkg"), "sub/m.py")
    assert res["path"] == "sub/m.py" and res["text"] == "m\n"
    # An absolute path under the subdir cwd comes back cwd-relative.
    assert read_file(str(repo / "pkg"), str(repo / "pkg" / "sub" / "m.py"))["path"] == "sub/m.py"


def test_path_above_a_subdir_cwd_is_refused(repo: Path) -> None:
    """Above the cwd is outside, even inside the same repo: the CLI never resolves
    ``@path`` there, so neither does the Files tab."""
    (repo / "pkg").mkdir()
    (repo / "x").write_text("x\n")
    assert _code(repo / "pkg", "../x") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo / "pkg", "../tracked.txt") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo / "pkg", str(repo / "tracked.txt")) == "PATH_OUTSIDE_WORKTREE"
    os.symlink("../tracked.txt", repo / "pkg" / "up.txt")
    assert _code(repo / "pkg", "up.txt") == "PATH_OUTSIDE_WORKTREE"
    (repo / "pkg" / "vendor" / ".git").mkdir(parents=True)
    (repo / "pkg" / "vendor" / ".git" / "config").write_text("x\n")
    assert _code(repo / "pkg", "vendor/.git/config") == "PATH_OUTSIDE_WORKTREE"


def test_absolute_path_inside_is_normalised(repo: Path) -> None:
    assert read_file(str(repo), str(repo / "tracked.txt"))["path"] == "tracked.txt"


def test_large_file_is_truncated_at_512_kib(repo: Path) -> None:
    assert MAX_READ_BYTES == 512 * 1024
    (repo / "big.txt").write_bytes(b"a" * (MAX_READ_BYTES + 100))
    res = read_file(str(repo), "big.txt")
    assert res["truncated"] is True
    assert len(res["text"]) == MAX_READ_BYTES
    assert res["size"] == MAX_READ_BYTES + 100


def test_binary_file(repo: Path) -> None:
    (repo / "img.bin").write_bytes(b"\x89PNG\x00\x01\x02" + b"z" * 100)
    res = read_file(str(repo), "img.bin")
    assert res["binary"] is True and res["text"] is None and res["size"] == 107


def test_symlink_inside_worktree_resolves(repo: Path) -> None:
    os.symlink("tracked.txt", repo / "link.txt")
    res = read_file(str(repo), "link.txt")
    assert res["path"] == "tracked.txt" and res["text"] == "hello\n"


def test_symlink_pointing_outside_is_refused(repo: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret\n")
    os.symlink(secret, repo / "out.txt")
    os.symlink(tmp_path, repo / "outdir")
    assert _code(repo, "out.txt") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "outdir/secret.txt") == "PATH_OUTSIDE_WORKTREE"


def test_dotdot_tilde_nul_and_absolute_outside_are_refused(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "x").write_text("x\n")
    assert _code(repo, "../x") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "sub/../../x") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "~/x") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "a\x00b") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, str(tmp_path / "x")) == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "/etc/passwd") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, "") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, ".") == "PATH_OUTSIDE_WORKTREE"


def test_git_internals_are_refused(repo: Path) -> None:
    assert _code(repo, ".git/config") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, ".GIT/config") == "PATH_OUTSIDE_WORKTREE"
    assert _code(repo, ".git") == "PATH_OUTSIDE_WORKTREE"
    os.symlink(".git/config", repo / "cfg")
    assert _code(repo, "cfg") == "PATH_OUTSIDE_WORKTREE"
    (repo / "vendor" / ".git").mkdir(parents=True)
    (repo / "vendor" / ".git" / "config").write_text("x\n")
    assert _code(repo, "vendor/.git/config") == "PATH_OUTSIDE_WORKTREE"


def test_fifo_is_refused_without_blocking(repo: Path) -> None:
    os.mkfifo(repo / "pipe")
    result: list[str] = []

    def run() -> None:
        try:
            read_file(str(repo), "pipe")
        except FileAccessError as exc:
            result.append(exc.code)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "reading a FIFO blocked"
    assert result == ["PATH_OUTSIDE_WORKTREE"]


def test_directory_is_refused(repo: Path) -> None:
    (repo / "d").mkdir()
    assert _code(repo, "d") == "PATH_OUTSIDE_WORKTREE"


def test_missing_file_is_not_found(repo: Path) -> None:
    assert _code(repo, "nope.txt") == "FILE_NOT_FOUND"
    assert _code(repo, "no/such/dir.txt") == "FILE_NOT_FOUND"
    assert _code(repo, "tracked.txt/child") == "FILE_NOT_FOUND"


def test_no_cwd_or_non_git_cwd_is_not_found(tmp_path: Path) -> None:
    assert _code("", "a.txt") == "FILE_NOT_FOUND"
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "a.txt").write_text("x\n")
    assert _code(tmp_path / "plain", "a.txt") == "FILE_NOT_FOUND"


def test_intermediate_symlink_swap_is_never_followed(repo: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory swapped for an outside symlink after containment was checked is not
    followed: the open walks components with O_NOFOLLOW."""
    (repo / "d").mkdir()
    (repo / "d" / "f.txt").write_text("in\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("out\n")
    real = worktree_files.worktree_rel

    def swap(path: str, cwd: str | None) -> str | None:
        rel = real(path, cwd)
        (repo / "d" / "f.txt").unlink()
        (repo / "d").rmdir()
        os.symlink(outside, repo / "d")
        return rel

    monkeypatch.setattr(worktree_files, "worktree_rel", swap)
    assert _code(repo, "d/f.txt") == "PATH_OUTSIDE_WORKTREE"


# ------------------------------------------------------------------ patterns


def test_worktree_rel_core(repo: Path, tmp_path: Path) -> None:
    assert worktree_rel("tracked.txt", str(repo)) == "tracked.txt"
    assert worktree_rel(str(repo / "a" / "b.txt"), str(repo)) == "a/b.txt"
    for bad in ("../x", "~/x", "a\x00b", str(tmp_path), ".", ""):
        assert worktree_rel(bad, str(repo)) is None
    assert worktree_rel("a.txt", None) is None
    assert worktree_rel_path("Edit", {"file_path": "a.txt"}, str(repo)) == "a.txt"
    assert worktree_rel_path("Edit", {"file_path": "a.txt", "notebook_path": "b"},
                             str(repo)) is None
