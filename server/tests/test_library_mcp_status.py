"""Settings > MCP read side: status rows from the table, Test endpoint, deadline path."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest
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
    from viberpit import accounts
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["bob"])
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

    async def enabled():
        return {"a"}

    async def live():
        return set()

    monkeypatch.setattr(library.mcp_store, "rows", fake_rows)
    monkeypatch.setattr(library, "_enabled_accounts", enabled)
    monkeypatch.setattr(library, "_live_session_ids", live)
    c = _client(None, monkeypatch)
    [s] = c.get("/api/library/mcp").json()["servers"]
    assert s["status"] == [{**ROWS[0], "counted": True}] and not any(k.lower() == "needsauth" for k in s)
    c = _client(None, monkeypatch, enabled=False)
    [s] = c.get("/api/library/mcp").json()["servers"]
    assert s["status"] == []


# ---- account-name hardening: a request-supplied account must be a KNOWN account ----

BAD_ACCOUNTS = ["../x", "../../etc", "a/b", ".", "", "ghost"]


@pytest.fixture
def guarded(monkeypatch):
    """Known accounts = {alice}; any filesystem/probe entry point reached with a bad
    name fails the test."""
    from viberpit import accounts, configdir, paths
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["alice"])

    def _boom(*a, **k):
        raise AssertionError("reached a filesystem/probe entry point with an unvalidated account")

    monkeypatch.setattr(configdir, "compose", _boom)
    monkeypatch.setattr(paths, "account_cfg_dir", _boom)

    async def _probe(account=None):
        _boom()

    class _FakeMgr:
        async def start(self, *a, **k):
            _boom()

    async def _no_rows():
        return []

    monkeypatch.setattr(library, "_status_rows", _no_rows)
    monkeypatch.setattr(library, "get_settings", lambda: SimpleNamespace(mcp_status_enabled=True))
    app = FastAPI()
    app.include_router(library.router)
    app.state.sessions = _Orch(_probe)
    app.state.mcp_auth = _FakeMgr()
    app.dependency_overrides[current_identity] = lambda: "tester"
    from server.db import get_db
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(library, "valid_mcp_name", lambda n: True)
    monkeypatch.setattr(library.scan, "list_mcp_servers",
                        lambda status_rows=None: {"servers": [{"name": "notion", "transport": "http"}]})
    return TestClient(app)


@pytest.mark.parametrize("bad", BAD_ACCOUNTS)
def test_test_endpoint_rejects_unknown_account(guarded, bad):
    r = guarded.post("/api/library/mcp/test", json={"account": bad})
    assert r.status_code == 400
    assert r.json()["detail"] == {"error": "unknown_account"}


@pytest.mark.parametrize("bad", BAD_ACCOUNTS)
def test_auth_start_rejects_unknown_account(guarded, bad):
    r = guarded.post("/api/library/mcp/notion/auth", json={"account": bad})
    assert r.status_code == 400
    assert r.json()["detail"] == {"error": "unknown_account"}


@pytest.mark.parametrize("bad", ["../x", "a/b", ".", "", "ghost", "..", "alice/../bob"])
def test_probe_cfg_dir_refuses_unknown_and_escaping_names(monkeypatch, tmp_path, bad):
    from server.mcp_health import probe
    from viberpit import accounts, paths
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["alice"])
    monkeypatch.setattr(paths, "cfg_root", lambda: tmp_path)
    with pytest.raises(probe.ProbeSkipped, match="unknown account"):
        probe._cfg_dir_for(bad)


def test_probe_cfg_dir_accepts_known_direct_child(monkeypatch, tmp_path):
    from server.mcp_health import probe
    from viberpit import accounts, paths
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["alice"])
    monkeypatch.setattr(paths, "cfg_root", lambda: tmp_path)
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / ".claude.json").write_text("{}")
    assert probe._cfg_dir_for("alice") == tmp_path / "alice"


@pytest.mark.asyncio
async def test_probe_reports_unknown_account_in_skipped(monkeypatch, tmp_path, isolated_db):
    from server.mcp_health import probe
    from viberpit import accounts, paths
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["alice"])
    monkeypatch.setattr(paths, "cfg_root", lambda: tmp_path)
    out = await probe.probe(["../x"])
    assert out["skipped"] == [{"account": "../x", "reason": "unknown account"}]


def test_picker_drops_preferred_names_not_known(monkeypatch, tmp_path):
    from server import mcp_auth
    from viberpit import accounts, configdir
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["alice"])
    composed: list[str] = []
    monkeypatch.setattr(configdir, "compose", lambda n: composed.append(n) or tmp_path / n)
    name, _ = mcp_auth.default_account_picker("notion", ["../x", "a/b", "", "alice"])
    assert name == "alice" and composed == ["alice"]


def test_picker_with_only_bad_preferred_and_no_accounts_raises(monkeypatch):
    from server import mcp_auth
    from viberpit import accounts, configdir
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: [])
    monkeypatch.setattr(configdir, "compose",
                        lambda n: (_ for _ in ()).throw(AssertionError("compose reached")))
    with pytest.raises(mcp_auth.McpAuthError):
        mcp_auth.default_account_picker("notion", ["../x"])


def _write_store(bundle: dict) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.shared_mcp_path(), {"aws|k": bundle})


def _overall_client(monkeypatch, rows, enabled=("a",), live=()):
    async def _rows():
        return rows

    async def _enabled():
        return set(enabled)

    async def _live():
        return set(live)

    monkeypatch.setattr(library, "_status_rows", _rows)
    monkeypatch.setattr(library, "_enabled_accounts", _enabled)
    monkeypatch.setattr(library, "_live_session_ids", _live)
    app = FastAPI()
    app.include_router(library.router)
    app.dependency_overrides[current_identity] = lambda: "tester"
    return TestClient(app)


def _write_aws() -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(),
                      {"mcpServers": {"aws": {"type": "http", "url": "https://u?x=1"}}})


def test_get_mcp_overall_ignores_disabled_account_row(shared: Path, monkeypatch):  # noqa: F811
    _write_aws()
    _write_store({"serverName": "aws", "serverUrl": "https://u?x=1", "accessToken": "t"})
    rows = [{"server": "aws", "account": "gone", "host": "dobby", "status": "needs-auth",
             "source": "probe"}]
    body = _overall_client(monkeypatch, rows).get("/api/library/mcp").json()
    [s] = body["servers"]
    assert s["overall"] == {"status": None, "error": None, "grant": "live"}
    assert s["status"][0]["counted"] is False


def test_get_mcp_overall_never_signed_in(shared: Path, monkeypatch):  # noqa: F811
    _write_aws()
    _write_store({"serverName": "aws", "serverUrl": "https://u?x=1", "accessToken": ""})
    rows = [{"server": "aws", "account": "a", "host": "dobby", "status": "connected",
             "source": "probe"}]
    [s] = _overall_client(monkeypatch, rows).get("/api/library/mcp").json()["servers"]
    assert s["overall"]["status"] == "needs-auth"
    assert s["overall"]["grant"] == "never"
    assert s["status"][0]["counted"] is True
