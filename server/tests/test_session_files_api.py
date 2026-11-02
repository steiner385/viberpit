"""GET /api/sessions/{id}/files and /files/content (D-7 @-files, Files tab, clickable
paths) and every layer under them: the orchestrator, the in-process facade, the daemon
RPC, RemoteOrchestrator and the fleet router. Both read the session's worktree, so they
run on the host that owns it — and a refused read keeps its ``FileAccessError`` code
across the RPC hop, so the edge still answers 400/404 with that code."""
from __future__ import annotations

import logging
import os
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
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import WORKTREE_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.sessions.worktree_files import (
    FILE_NOT_FOUND,
    PATH_OUTSIDE_WORKTREE,
    FileAccessError,
)
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.ipc_harness import serve_daemon_uds

SID = "22222222-2222-4222-8222-222222222222"
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a committed file in a subdirectory plus an untracked file."""
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "src" / "app.py").write_text("print('hi')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")
    (work / "notes.md").write_text("# notes\n")
    (tmp_path / "secret.txt").write_text("outside\n")
    return work


def _rec(cwd: str | None) -> SessionRecord:
    return SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c", cols=80, rows=24, cwd=cwd)  # type: ignore[arg-type]


def _client(cwd: str | None):
    """A real app over a real orchestrator holding one record, so the request goes
    route -> InProcessOrchestratorAdapter -> SessionOrchestrator.session_files."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = _rec(cwd)
    return TestClient(app, raise_server_exceptions=False)


# --- HTTP: list ---------------------------------------------------------------

def test_lists_worktree_files(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/files", headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"files", "truncated"}
    assert {f["path"] for f in body["files"]} == {"src/app.py", "notes.md"}
    assert body["truncated"] is False


def test_query_filters_and_limit_caps(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        q = c.get(f"/api/sessions/{SID}/files?q=app", headers=_H).json()
        one = c.get(f"/api/sessions/{SID}/files?limit=1", headers=_H).json()
    assert [f["path"] for f in q["files"]] == ["src/app.py"]
    assert len(one["files"]) == 1


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_limit_out_of_range_is_422(repo: Path, limit: int) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/files?limit={limit}", headers=_H)
    assert r.status_code == 422


def test_query_is_clamped_to_256_chars(repo: Path) -> None:
    """An over-long ``q`` is cut at the endpoint before it reaches the ranking."""
    seen: list[str] = []

    async def fake(self, session_id, *, q="", limit=50):
        seen.append(q)
        return {"files": [], "truncated": False}

    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, "session_files", fake):
        r = c.get(f"/api/sessions/{SID}/files", params={"q": "a" * 5000}, headers=_H)
    assert r.status_code == 200
    assert seen == ["a" * 256]


def test_no_cwd_lists_nothing() -> None:
    with patch.dict(os.environ, _ENV), _client(None) as c:
        r = c.get(f"/api/sessions/{SID}/files", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"files": [], "truncated": False}


def test_list_unknown_session_404(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get("/api/sessions/nope/files", headers=_H).status_code == 404


# --- HTTP: content -------------------------------------------------------------

def test_reads_file_content(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/files/content", params={"path": "src/app.py"},
                  headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "src/app.py", "text": "print('hi')\n", "binary": False,
                        "truncated": False, "size": 12}


@pytest.mark.parametrize("path", ["../secret.txt", ".git/config", "/etc/passwd"])
def test_escape_is_400_with_code(repo: Path, path: str) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/files/content", params={"path": path}, headers=_H)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == PATH_OUTSIDE_WORKTREE


def test_missing_file_is_404_with_code(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/files/content", params={"path": "gone.txt"},
                  headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == FILE_NOT_FOUND


def test_no_cwd_content_is_404_with_code() -> None:
    with patch.dict(os.environ, _ENV), _client(None) as c:
        r = c.get(f"/api/sessions/{SID}/files/content", params={"path": "a"}, headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == FILE_NOT_FOUND


def test_unexpected_access_code_fails_safe_as_400(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, "session_file",
                         side_effect=FileAccessError("SOMETHING_NEW")):
        r = c.get(f"/api/sessions/{SID}/files/content", params={"path": "a"}, headers=_H)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "SOMETHING_NEW"


def test_content_unknown_session_404(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get("/api/sessions/nope/files/content", params={"path": "notes.md"},
                  headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"] == "session not found"


def test_content_requires_path(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get(f"/api/sessions/{SID}/files/content", headers=_H).status_code == 422


@pytest.mark.parametrize("url", [f"/api/sessions/{SID}/files",
                                 f"/api/sessions/{SID}/files/content?path=notes.md"])
def test_requires_identity(repo: Path, url: str) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get(url).status_code == 401


@pytest.mark.parametrize(("method", "url"), [
    ("session_files", f"/api/sessions/{SID}/files"),
    ("session_file", f"/api/sessions/{SID}/files/content?path=notes.md"),
])
def test_daemon_without_the_rpc_is_daemon_outdated_409(repo: Path, method: str,
                                                        url: str) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, method,
                         side_effect=DaemonError("UnknownMethod", method)):
        r = c.get(url, headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "daemon_outdated"


def test_other_daemon_errors_keep_the_app_mapping(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c, \
            patch.object(SessionOrchestrator, "session_file",
                         side_effect=DaemonError("RuntimeError", "boom")):
        r = c.get(f"/api/sessions/{SID}/files/content?path=a", headers=_H)
    assert r.status_code == 409
    assert r.json() == {"detail": "boom"}


# --- RemoteOrchestrator / RPC / fleet ----------------------------------------

class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs, timeout))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_same_kwargs() -> None:
    client = _RecordingClient(result={"files": [], "truncated": False})
    ro = RemoteOrchestrator(client)
    await ro.session_files(SID, q="ap", limit=7)
    await ro.session_file(SID, path="a/b.py")
    assert client.calls == [
        ("session_files", {"session_id": SID, "q": "ap", "limit": 7}, WORKTREE_RPC_TIMEOUT),
        ("session_file", {"session_id": SID, "path": "a/b.py"}, WORKTREE_RPC_TIMEOUT),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session_files", "session_file"])
async def test_remote_reraises_unknown_session_as_keyerror(method: str) -> None:
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError("KeyError", "nope")))
    kw = {"path": "x"} if method == "session_file" else {}
    with pytest.raises(KeyError):
        await getattr(ro, method)("nope", **kw)


@pytest.mark.asyncio
async def test_remote_rebuilds_file_access_error_from_envelope() -> None:
    err = DaemonError("FileAccessError", FILE_NOT_FOUND, {"code": FILE_NOT_FOUND})
    ro = RemoteOrchestrator(_RecordingClient(error=err))
    with pytest.raises(FileAccessError) as ei:
        await ro.session_file(SID, path="x")
    assert ei.value.code == FILE_NOT_FOUND


@pytest.mark.asyncio
async def test_remote_does_not_trust_an_unknown_code() -> None:
    """A code outside the two the edge maps is not invented into a FileAccessError."""
    err = DaemonError("FileAccessError", "WAT", {"code": "WAT"})
    ro = RemoteOrchestrator(_RecordingClient(error=err))
    with pytest.raises(DaemonError):
        await ro.session_file(SID, path="x")


@pytest.mark.asyncio
async def test_rpc_round_trip_runs_on_the_daemon(
        repo: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Over the real daemon RPC: the daemon's orchestrator lists and reads from its own
    record's cwd; refusals keep their FileAccessError code; an unknown session comes
    back as a KeyError. None of those expected refusals logs a WARNING ``rpc.error``
    on the daemon (WARNING auto-files an issue)."""
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
            listed = await ro.session_files(SID, q="app", limit=5)
            content = await ro.session_file(SID, path="notes.md")
            with pytest.raises(FileAccessError) as outside:
                await ro.session_file(SID, path="../secret.txt")
            with pytest.raises(FileAccessError) as missing:
                await ro.session_file(SID, path="gone.txt")
            with pytest.raises(KeyError):
                await ro.session_files("nope")
            with pytest.raises(KeyError):
                await ro.session_file("nope", path="notes.md")
            with pytest.raises(KeyError):
                await ro.session_diff("nope")
        finally:
            await client.aclose()
    assert listed == {"files": [{"path": "src/app.py"}], "truncated": False}
    assert content["text"] == "# notes\n" and content["path"] == "notes.md"
    assert outside.value.code == PATH_OUTSIDE_WORKTREE
    assert missing.value.code == FILE_NOT_FOUND
    rpc_errors = [r for r in caplog.records
                  if r.name == "server.ipc.daemon" and "rpc.error" in r.getMessage()]
    assert len(rpc_errors) == 5  # 2 FileAccessError + 3 unknown-session KeyError
    assert all(r.levelno == logging.INFO for r in rpc_errors), \
        [(r.levelname, r.getMessage()) for r in rpc_errors]


def test_file_access_error_is_an_expected_daemon_refusal() -> None:
    """It carries ``error_data``: the daemon logs it at INFO (WARNING would auto-file an
    issue for every mistyped path) and the envelope carries the code."""
    assert FileAccessError(FILE_NOT_FOUND).error_data == {"code": FILE_NOT_FOUND}


class _HostOrch:
    def __init__(self, name):
        self.name, self.calls = name, []

    async def session_files(self, session_id, **kw):
        self.calls.append(("files", session_id, kw))
        return {"host": self.name}

    async def session_file(self, session_id, **kw):
        self.calls.append(("file", session_id, kw))
        return {"host": self.name}


class _Router:
    def __init__(self, orchs):
        self._orchs = orchs

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)


@pytest.mark.asyncio
async def test_fleet_routes_to_owning_host() -> None:
    assert {"session_files", "session_file"} <= _PER_SESSION
    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert await fo.session_files(SID, q="x", limit=3) == {"host": "rocky"}
    assert await fo.session_file(SID, path="a.py") == {"host": "rocky"}
    assert rocky.calls == [("files", SID, {"q": "x", "limit": 3}),
                           ("file", SID, {"path": "a.py"})]
    assert dobby.calls == []
