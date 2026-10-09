"""Settings > MCP read side: status rows from the table, Test endpoint, deadline path."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api import library
from server.auth.middleware import current_identity
from server.ipc.client import DaemonTimeout
from server.library import scan
from server.sessions.facade import rpc_deadline
from server.tests.test_library_scan import _seed_plugin, shared  # noqa: F401 — fixture


def _write_user_server(name: str = "notion") -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(),
                      {"mcpServers": {name: {"type": "http", "url": "https://x"}}})


def test_list_attaches_rows_by_short_name(shared: Path):  # noqa: F811
    _write_user_server()
    rows = [{"server": "notion", "account": "a", "host": "dobby", "status": "needs-auth"},
            {"server": "plugin:p:other", "account": "a", "host": "dobby", "status": "connected"}]
    [s] = scan.list_mcp_servers(status_rows=rows)["servers"]
    assert s["status"] == [rows[0]] and not any(k.lower() == "needsauth" for k in s)


def test_plugin_key_rows_match_short_name(shared: Path):  # noqa: F811
    _seed_plugin(shared, "alpha")
    rows = [{"server": "plugin:alpha:alpha-srv", "account": "a", "host": "dobby", "status": "needs-auth"}]
    s = next(x for x in scan.list_mcp_servers(status_rows=rows)["servers"] if x["name"] == "alpha-srv")
    assert s["status"] == rows


def test_cache_reader_is_gone():
    assert not any(n.startswith("_needs_auth") for n in dir(scan))


ROWS = [
    {"server": "notion", "account": "a", "host": "dobby", "status": "connected"},
    {"server": "plugin:p:other", "account": "a", "host": "dobby", "status": "failed"},
]


class _Orch:
    def __init__(self, fn):
        self.probe_mcp = fn


def _client(orch, monkeypatch, enabled: bool = True) -> TestClient:
    monkeypatch.setattr(library, "get_settings", lambda: SimpleNamespace(mcp_status_enabled=enabled))
    app = FastAPI()
    app.include_router(library.router)
    app.state.sessions = orch
    app.dependency_overrides[current_identity] = lambda: "tester"
    return TestClient(app)


def test_test_endpoint_filters_to_server(monkeypatch):
    async def probe(account=None):
        return {"rows": list(ROWS), "skipped": [{"account": "z", "reason": "busy"}]}

    r = _client(_Orch(probe), monkeypatch).post("/api/library/mcp/test", json={"server": "notion"})
    assert r.status_code == 200
    assert r.json() == {"rows": [ROWS[0]], "skipped": [{"account": "z", "reason": "busy"}]}


def test_test_endpoint_disabled_short_circuits(monkeypatch):
    def boom(account=None):
        raise AssertionError("must not probe")

    r = _client(_Orch(boom), monkeypatch, enabled=False).post("/api/library/mcp/test", json={})
    assert r.json() == {"rows": [], "skipped": []}


def test_marked_callee_gets_deadline_pushed_in_not_cancelled(monkeypatch):
    seen: dict = {}

    @rpc_deadline
    async def probe(account=None, *, timeout=None):
        seen["timeout"] = timeout
        seen["account"] = account
        return {"rows": [], "skipped": []}

    r = _client(_Orch(probe), monkeypatch).post("/api/library/mcp/test", json={"account": "bob"})
    assert r.status_code == 200
    assert seen == {"timeout": 35.0, "account": "bob"}


def test_daemon_timeout_is_504(monkeypatch):
    @rpc_deadline
    async def probe(account=None, *, timeout=None):
        raise DaemonTimeout("slow")

    r = _client(_Orch(probe), monkeypatch).post("/api/library/mcp/test", json={})
    assert r.status_code == 504
    assert r.json()["detail"] == {"error": "mcp_probe_timeout"}


def test_deadline_marker_present_end_to_end():
    """The orchestrator the route reaches in remote mode must carry @rpc_deadline, or
    call_with_deadline falls back to wait_for and cancels the RPC (#734)."""
    from server.sessions.facade import _supports_native_deadline
    from server.sessions.fleet_orchestrator import FleetOrchestrator
    from server.sessions.remote import RemoteOrchestrator

    assert _supports_native_deadline(RemoteOrchestrator.probe_mcp)
    fleet = FleetOrchestrator.__new__(FleetOrchestrator)
    assert _supports_native_deadline(fleet.probe_mcp)


def test_get_returns_status_and_kill_switch_empties_it(shared, monkeypatch):  # noqa: F811
    _write_user_server()

    async def fake_rows():
        return [ROWS[0]]

    monkeypatch.setattr(library.mcp_store, "rows", fake_rows)
    c = _client(None, monkeypatch)
    [s] = c.get("/api/library/mcp").json()["servers"]
    assert s["status"] == [ROWS[0]] and not any(k.lower() == "needsauth" for k in s)
    c = _client(None, monkeypatch, enabled=False)
    [s] = c.get("/api/library/mcp").json()["servers"]
    assert s["status"] == []
