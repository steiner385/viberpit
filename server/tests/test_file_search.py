"""Content search over a session's worktree (D-20): ``worktree_files.search_files`` on
a real temporary git repo, then the route, RPC and fleet layers above it."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import worktree_files as wf
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import WORKTREE_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.ipc_harness import serve_daemon_uds

SID = "33333333-3333-4333-8333-333333333333"
_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, **_GIT_ENV, "HOME": str(cwd)}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def _commit(work: Path) -> None:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "src" / "app.py").write_text("import os\nprint('Hello World')\nx = a.b(c)\n")
    (work / "notes.md").write_text("hello lowercase\n-dash start\n")
    (work / ".gitignore").write_text("ignored.txt\n")
    _commit(work)
    (work / "untracked.txt").write_text("hello untracked\n")
    (work / "ignored.txt").write_text("hello ignored\n")
    return work


def _paths(res: dict) -> set[str]:
    return {m["path"] for m in res["matches"]}


# --- search_files -------------------------------------------------------------

def test_match_shape_and_offsets(repo: Path) -> None:
    res = wf.search_files(str(repo), "World")
    assert res == {"matches": [{"path": "src/app.py", "line": 2, "text": "print('Hello World')",
                                "start": 13, "end": 18}],
                   "truncated": False, "timedOut": False}


def test_smart_case(repo: Path) -> None:
    lower = wf.search_files(str(repo), "hello")
    assert _paths(lower) == {"src/app.py", "notes.md"}  # all-lowercase: insensitive
    assert _paths(wf.search_files(str(repo), "Hello")) == {"src/app.py"}  # has upper: exact


def test_fixed_string_vs_regex(repo: Path) -> None:
    assert wf.search_files(str(repo), "a.b(c)")["matches"][0]["line"] == 3
    assert wf.search_files(str(repo), "a.b(c")["matches"][0]["line"] == 3  # not a regex
    rx = wf.search_files(str(repo), r"Hel+o W", regex=True)
    # regex mode never re-runs the pattern in Python: git's column only, an empty span
    assert rx["matches"][0]["start"] == 7 and rx["matches"][0]["end"] == 7
    assert wf.search_files(str(repo), "a.b(c)", regex=True)["matches"] == []  # group, no literal ()


def test_pathological_regex_cannot_stall_the_loop(repo: Path) -> None:
    import asyncio
    import time

    (repo / "evil.txt").write_text("a" * 40 + "c\n")
    _commit(repo)
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def run() -> dict:
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        res = await asyncio.to_thread(wf.search_files, str(repo), "(a+)+b|a", True)
        elapsed = time.monotonic() - t0
        t.cancel()
        res["elapsed"] = elapsed
        return res

    res = asyncio.run(run())
    assert res["elapsed"] < wf.SEARCH_TIMEOUT_S + 2
    assert res["matches"] and res["matches"][0]["end"] == res["matches"][0]["start"]
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.5, max(gaps)  # the loop kept ticking throughout


def test_nul_in_query_is_bad_query(repo: Path) -> None:
    for regex in (False, True):
        with pytest.raises(wf.FileAccessError) as ei:
            wf.search_files(str(repo), "a\x00b", regex)
        assert ei.value.code == wf.BAD_QUERY == "BAD_QUERY"


def test_stream_git_survives_a_nul_argument(repo: Path) -> None:
    from viberpit.worktree_diff import stream_git
    assert stream_git(str(repo), ["grep", "-e", "a\x00b"], lambda c: False) == "failed"


def test_other_git_failures_are_search_failed_not_bad_regex(repo: Path) -> None:
    _git(repo, "config", "core.bigFileThreshold", "bogus")  # harmless; real failure below
    (repo / ".git" / "index").write_bytes(b"garbage")  # git cannot read the index
    for regex in (False, True):
        with pytest.raises(wf.FileAccessError) as ei:
            wf.search_files(str(repo), "hello", regex)
        assert ei.value.code == wf.SEARCH_FAILED == "SEARCH_FAILED"


def test_submodule_recurse_is_pinned_off(repo: Path) -> None:
    seen: list[list[str]] = []

    def fake(cwd, args, consume, **kw):
        seen.append(list(args))
        return "ok"

    with patch.object(wf, "stream_git", fake):
        wf.search_files(str(repo), "hello")
    assert "submodule.recurse=false" in seen[0]
    assert seen[0].index("submodule.recurse=false") < seen[0].index("grep")


def test_bad_regex(repo: Path) -> None:
    with pytest.raises(wf.FileAccessError) as ei:
        wf.search_files(str(repo), "a(b", regex=True)
    assert ei.value.code == wf.BAD_REGEX == "BAD_REGEX"
    assert wf.search_files(str(repo), "a(b")["timedOut"] is False  # fixed string is fine


def test_query_starting_with_dash_is_not_a_flag(repo: Path) -> None:
    res = wf.search_files(str(repo), "-dash")
    assert [(m["path"], m["line"], m["start"]) for m in res["matches"]] == [("notes.md", 2, 0)]
    assert wf.search_files(str(repo), "--version")["matches"] == []
    assert wf.search_files(str(repo), "--no-index", regex=True)["matches"] == []


def test_only_tracked_files_are_searched(repo: Path) -> None:
    paths = _paths(wf.search_files(str(repo), "hello"))
    assert "untracked.txt" not in paths and "ignored.txt" not in paths


def test_short_query_finds_nothing_and_missing_worktree_is_not_found(
        repo: Path, tmp_path: Path) -> None:
    assert wf.search_files(str(repo), "h")["matches"] == []
    for bad in (str(tmp_path / "nope"), "", str(tmp_path)):
        with pytest.raises(wf.FileAccessError) as ei:
            wf.search_files(bad, "hello")
        assert ei.value.code == wf.FILE_NOT_FOUND


def test_paths_are_relative_to_a_subdirectory_cwd(repo: Path) -> None:
    res = wf.search_files(str(repo / "src"), "hello")
    assert _paths(res) == {"app.py"}  # nothing above cwd, and read_file-relative


def test_symlink_blobs_are_not_followed(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("hello SECRETTOKEN\n")
    os.symlink(outside / "secret.txt", repo / "link.txt")
    os.symlink(outside, repo / "linkdir")
    _commit(repo)
    assert wf.search_files(str(repo), "SECRETTOKEN")["matches"] == []


def _outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    (out / "b").mkdir(parents=True)
    (out / "secret.txt").write_text("SECRETTOKEN\n")
    (out / "b" / "secret.txt").write_text("SECRETTOKEN\n")
    return out


def test_swapped_leading_directory_symlink_leaks_nothing(repo: Path, tmp_path: Path) -> None:
    """A tracked ``dir/secret.txt`` whose ``dir`` was swapped for a symlink (uncommitted):
    git only lstats the last component, so it would print the outside file."""
    (repo / "dir").mkdir()
    (repo / "dir" / "secret.txt").write_text("SECRETTOKEN tracked\n")
    _commit(repo)
    shutil.rmtree(repo / "dir")
    os.symlink(_outside(tmp_path), repo / "dir")
    res = wf.search_files(str(repo), "SECRETTOKEN")
    assert res["matches"] == [] and res["truncated"] is False
    assert "SECRETTOKEN" not in repr(res)
    # the legitimate matches in other files still arrive
    assert _paths(wf.search_files(str(repo), "hello"))


def test_nested_symlinked_component_leaks_nothing(repo: Path, tmp_path: Path) -> None:
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "secret.txt").write_text("SECRETTOKEN tracked\n")
    (repo / "keep.txt").write_text("SECRETTOKEN keep\n")
    _commit(repo)
    shutil.rmtree(repo / "a")
    os.symlink(_outside(tmp_path), repo / "a")
    assert _paths(wf.search_files(str(repo), "SECRETTOKEN")) == {"keep.txt"}


def test_swapped_dir_symlink_inside_the_worktree_is_also_dropped(repo: Path) -> None:
    """Even a symlink to another directory *inside* the worktree is refused: the same
    rule as ``read_file`` (no symlinked component anywhere)."""
    (repo / "d").mkdir()
    (repo / "d" / "x.txt").write_text("INSIDE\n")
    _commit(repo)
    shutil.rmtree(repo / "d")
    os.symlink(repo / "src", repo / "d")
    assert wf.search_files(str(repo), "INSIDE")["matches"] == []


def test_binary_files_are_skipped(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\x00\x01hello binary\x00")
    _commit(repo)
    assert "blob.bin" not in _paths(wf.search_files(str(repo), "hello"))


def test_non_utf8_path_is_skipped_and_non_utf8_text_replaced(repo: Path) -> None:
    (repo / "latin.txt").write_bytes(b"caf\xe9 hello\n")
    _commit(repo)
    m = [x for x in wf.search_files(str(repo), "hello")["matches"] if x["path"] == "latin.txt"]
    assert m and m[0]["text"] == "caf� hello" and m[0]["start"] == 5


def test_match_cap_is_200(repo: Path) -> None:
    (repo / "many").mkdir()
    for i in range(30):
        (repo / "many" / f"f{i}.txt").write_text("needle\n" * 15)
    _commit(repo)
    res = wf.search_files(str(repo), "needle")
    assert len(res["matches"]) == wf.MAX_SEARCH_MATCHES and res["truncated"] is True
    small = wf.search_files(str(repo), "needle", limit=5)
    assert len(small["matches"]) == 5 and small["truncated"] is True


def test_file_cap_is_50(repo: Path) -> None:
    (repo / "wide").mkdir()
    for i in range(60):
        (repo / "wide" / f"f{i:02}.txt").write_text("needle\n")
    _commit(repo)
    res = wf.search_files(str(repo), "needle")
    assert len(_paths(res)) == wf.MAX_SEARCH_FILES and res["truncated"] is True


def test_per_file_cap_keeps_one_noisy_file_from_eating_the_budget(repo: Path) -> None:
    (repo / "noisy.txt").write_text("needle\n" * 500)
    _commit(repo)
    res = wf.search_files(str(repo), "needle")
    assert len(res["matches"]) == wf.MAX_MATCHES_PER_FILE and res["truncated"] is False


def test_long_line_is_truncated_around_the_match(repo: Path) -> None:
    (repo / "long.txt").write_text("x" * 1000 + "NEEDLE" + "y" * 1000 + "\n")
    (repo / "head.txt").write_text("NEEDLE" + "y" * 1000 + "\n")
    _commit(repo)
    by = {m["path"]: m for m in wf.search_files(str(repo), "NEEDLE")["matches"]}
    for m in by.values():
        assert len(m["text"]) == wf.MAX_SEARCH_LINE_CHARS
        assert m["text"][m["start"]:m["end"]] == "NEEDLE"
    assert by["head.txt"]["start"] == 0


def test_timeout_returns_partial_results(repo: Path) -> None:
    def slow(cwd, args, consume, **kw):
        assert kw["timeout"] == wf.SEARCH_TIMEOUT_S == 5.0
        consume(b"src/app.py\x002\x001\x00partial\n")
        return "timeout"

    with patch.object(wf, "stream_git", slow):
        res = wf.search_files(str(repo), "partial")
    assert res["timedOut"] is True and [m["text"] for m in res["matches"]] == ["partial"]


def test_search_never_touches_the_index(repo: Path) -> None:
    index = repo / ".git" / "index"
    os.utime(repo / "notes.md", (1, 1))  # a stat-dirty file would tempt a refresh
    before = index.stat().st_mtime_ns, index.read_bytes()
    wf.search_files(str(repo), "hello")
    wf.search_files(str(repo), "hel+o", regex=True)
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before
    assert not (repo / ".git" / "index.lock").exists()


def test_repo_grep_config_cannot_reshape_output(repo: Path) -> None:
    _git(repo, "config", "grep.fullName", "true")
    _git(repo, "config", "grep.patternType", "perl")
    res = wf.search_files(str(repo / "src"), "a.b(c)")
    assert [m["path"] for m in res["matches"]] == ["app.py"]


# --- route / orchestrator ------------------------------------------------------

def _rec(cwd: str | None) -> SessionRecord:
    return SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c", cols=80, rows=24, cwd=cwd)  # type: ignore[arg-type]


def _client(cwd: str | None):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = _rec(cwd)
    return TestClient(app, raise_server_exceptions=False)


def _url(**params: str) -> str:
    return f"/api/sessions/{SID}/files/search?" + "&".join(f"{k}={v}" for k, v in params.items())


def test_route_returns_matches(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(_url(q="World"), headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"matches", "truncated", "timedOut"}
    assert body["matches"][0]["path"] == "src/app.py"


def test_route_regex_flag_and_bad_regex_400(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        ok = c.get(_url(q="Hel%2Bo", regex="true"), headers=_H)
        bad = c.get(_url(q="a(b", regex="true"), headers=_H)
    assert ok.status_code == 200 and ok.json()["matches"]
    assert bad.status_code == 400 and bad.json()["detail"] == {"code": "BAD_REGEX"}


@pytest.mark.parametrize("params", [{}, {"q": "a"}, {"q": "ab", "limit": "0"},
                                    {"q": "ab", "limit": "201"}])
def test_route_validates_query_and_limit_422(repo: Path, params: dict) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get(_url(**params), headers=_H).status_code == 422


def test_route_clamps_query_and_passes_limit(repo: Path) -> None:
    seen: list[dict] = []

    async def fake(self, session_id, **kw):
        seen.append(kw)
        return {"matches": [], "truncated": False, "timedOut": False}

    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, "session_file_search", fake):
        r = c.get(_url(q="a" * 5000, limit="7"), headers=_H)
    assert r.status_code == 200
    assert seen == [{"q": "a" * wf.MAX_QUERY_CHARS, "regex": False, "limit": 7}]


def test_route_timeout_is_200_with_flag(repo: Path) -> None:
    def slow(cwd, args, consume, **kw):
        return "timeout"

    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(wf, "stream_git", slow):
        r = c.get(_url(q="hello"), headers=_H)
    assert r.status_code == 200
    assert r.json() == {"matches": [], "truncated": False, "timedOut": True}


def test_route_nul_is_400_bad_query_and_search_failed_is_500(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        nul = c.get(_url(q="a%00b"), headers=_H)
        (repo / ".git" / "index").write_bytes(b"garbage")
        broken = c.get(_url(q="hello"), headers=_H)
    assert nul.status_code == 400 and nul.json()["detail"] == {"code": "BAD_QUERY"}
    assert broken.status_code == 500 and broken.json()["detail"] == {"code": "SEARCH_FAILED"}


def test_route_missing_worktree_and_unknown_session_are_404(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(None) as c:
        nocwd = c.get(_url(q="hello"), headers=_H)
        unknown = c.get(f"/api/sessions/{'4' * 8}-4444-4444-8444-{'4' * 12}/files/search?q=hello",
                        headers=_H)
    assert nocwd.status_code == 404 and nocwd.json()["detail"] == {"code": "FILE_NOT_FOUND"}
    assert unknown.status_code == 404


def test_route_requires_identity(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get(_url(q="hello")).status_code == 401


def test_route_old_daemon_is_409(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, "session_file_search",
                         side_effect=DaemonError("UnknownMethod", "session_file_search")):
        r = c.get(_url(q="hello"), headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


# --- remote / RPC / fleet --------------------------------------------------------

class _RecordingClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs, timeout))
        if self.error is not None:
            raise self.error
        return {"matches": [], "truncated": False, "timedOut": False}


@pytest.mark.asyncio
async def test_remote_forwards_and_maps_errors() -> None:
    client = _RecordingClient()
    await RemoteOrchestrator(client).session_file_search(SID, q="ab", regex=True, limit=9)
    assert client.calls == [("session_file_search",
                             {"session_id": SID, "q": "ab", "regex": True, "limit": 9},
                             WORKTREE_RPC_TIMEOUT)]
    err = DaemonError("FileAccessError", wf.BAD_REGEX, {"code": wf.BAD_REGEX})
    with pytest.raises(wf.FileAccessError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=err)).session_file_search(SID, q="(")
    assert ei.value.code == wf.BAD_REGEX
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(error=DaemonError("KeyError", "x"))
                                 ).session_file_search("nope", q="ab")


@pytest.mark.asyncio
async def test_rpc_round_trip_and_expected_refusals_log_at_info(
        repo: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="server.ipc.daemon")
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    orch._records[SID] = _rec(str(repo))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            found = await ro.session_file_search(SID, q="World")
            with pytest.raises(wf.FileAccessError) as bad:
                await ro.session_file_search(SID, q="a(b", regex=True)
            with pytest.raises(KeyError):
                await ro.session_file_search("nope", q="ab")
        finally:
            await client.aclose()
    assert found["matches"][0]["path"] == "src/app.py"
    assert bad.value.code == wf.BAD_REGEX
    rpc_errors = [r for r in caplog.records
                  if r.name == "server.ipc.daemon" and "rpc.error" in r.getMessage()]
    assert len(rpc_errors) == 2 and all(r.levelno == logging.INFO for r in rpc_errors)


@pytest.mark.asyncio
async def test_fleet_routes_to_owning_host() -> None:
    assert "session_file_search" in _PER_SESSION

    class _Host:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def session_file_search(self, session_id, **kw):
            self.calls.append((session_id, kw))
            return {"host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _Host("dobby"), _Host("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert await fo.session_file_search(SID, q="x", regex=True) == {"host": "rocky"}
    assert rocky.calls == [(SID, {"q": "x", "regex": True})]
    assert dobby.calls == []
