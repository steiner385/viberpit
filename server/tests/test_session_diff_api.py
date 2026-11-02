"""GET /api/sessions/{id}/diff (D-4 Changes pane) and every layer under it: the
orchestrator, the in-process facade, the daemon RPC, RemoteOrchestrator and the fleet
router. The diff must be computed on the host that owns the session's worktree."""
from __future__ import annotations

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
from server.sessions.remote import DIFF_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.ipc_harness import serve_daemon_uds

SID = "11111111-1111-4111-8111-111111111111"
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
    """A repo with one committed file, then modified plus one untracked file. No
    origin, so the base is HEAD and the diff is the uncommitted changes."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")
    (work / "a.txt").write_text("one\nTWO\nthree\n")
    (work / "new.txt").write_text("fresh\n")
    return work


def _rec(cwd: str | None) -> SessionRecord:
    return SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c", cols=80, rows=24, cwd=cwd)  # type: ignore[arg-type]


def _client(cwd: str | None):
    """A real app over a real orchestrator holding one record, so the request goes
    route -> InProcessOrchestratorAdapter -> SessionOrchestrator.session_diff."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = _rec(cwd)
    return TestClient(app, raise_server_exceptions=False)


# --- HTTP -------------------------------------------------------------------

def test_real_diff(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/diff", headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"base", "head", "files", "additions", "deletions", "truncated"}
    files = {f["path"]: f for f in body["files"]}
    assert files["a.txt"]["status"] == "modified"
    assert files["a.txt"]["hunks"] and files["a.txt"]["hunks"][0]["lines"]
    assert files["new.txt"]["status"] == "added"
    assert body["additions"] == 2 and body["deletions"] == 1
    assert body["base"] is None and body["head"]


def test_summary_has_no_hunks(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get(f"/api/sessions/{SID}/diff?summary=1", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert {f["path"] for f in body["files"]} == {"a.txt", "new.txt"}
    assert all(f["hunks"] == [] for f in body["files"])
    assert body["additions"] == 2 and body["deletions"] == 1


def test_ignore_whitespace_flag_reaches_git(repo: Path) -> None:
    (repo / "a.txt").write_text("one\ntwo  \nthree\n")  # whitespace-only edit
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        plain = c.get(f"/api/sessions/{SID}/diff", headers=_H).json()
        ws = c.get(f"/api/sessions/{SID}/diff?ignoreWhitespace=1", headers=_H).json()
    assert "a.txt" in {f["path"] for f in plain["files"]}
    assert "a.txt" not in {f["path"] for f in ws["files"]}


def test_unknown_session_404(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        r = c.get("/api/sessions/nope/diff", headers=_H)
    assert r.status_code == 404


def test_no_cwd_is_empty_200() -> None:
    with patch.dict(os.environ, _ENV), _client(None) as c:
        r = c.get(f"/api/sessions/{SID}/diff", headers=_H)
    assert r.status_code == 200
    assert r.json()["files"] == [] and r.json()["base"] is None


def test_non_git_cwd_is_empty_200(tmp_path: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(tmp_path)) as c:
        r = c.get(f"/api/sessions/{SID}/diff", headers=_H)
    assert r.status_code == 200 and r.json()["files"] == []


def test_requires_identity(repo: Path) -> None:
    with patch.dict(os.environ, _ENV), _client(str(repo)) as c:
        assert c.get(f"/api/sessions/{SID}/diff").status_code == 401


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
    client = _RecordingClient(result={"files": []})
    ro = RemoteOrchestrator(client)
    assert await ro.session_diff(SID, summary=True, ignore_whitespace=True) == {"files": []}
    assert client.calls == [("session_diff",
                             {"session_id": SID, "summary": True, "ignore_whitespace": True},
                             DIFF_RPC_TIMEOUT)]


@pytest.mark.asyncio
async def test_remote_reraises_unknown_session_as_keyerror() -> None:
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError("KeyError", "nope")))
    with pytest.raises(KeyError):
        await ro.session_diff("nope")


@pytest.mark.asyncio
async def test_rpc_round_trip_runs_on_the_daemon(repo: Path) -> None:
    """Over the real daemon RPC: the daemon's orchestrator computes the diff from its
    own record's cwd, and an unknown session comes back as a KeyError."""
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    orch._records[SID] = _rec(str(repo))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            d = await ro.session_diff(SID, summary=True)
            with pytest.raises(KeyError):
                await ro.session_diff("nope")
        finally:
            await client.aclose()
    assert {f["path"] for f in d["files"]} == {"a.txt", "new.txt"}
    assert all(f["hunks"] == [] for f in d["files"])


class _HostOrch:
    def __init__(self, name):
        self.name, self.calls = name, []

    async def session_diff(self, session_id, **kw):
        self.calls.append((session_id, kw))
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
    assert "session_diff" in _PER_SESSION
    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert await fo.session_diff(SID, summary=True, ignore_whitespace=False) == {"host": "rocky"}
    assert rocky.calls == [(SID, {"summary": True, "ignore_whitespace": False})]
    assert dobby.calls == []
