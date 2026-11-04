"""Edge grant reconcile (Phase 2 Task 4): move grants to current keys, report what to purge."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from server.library import scan
from server.mcp_health import grants
from server.tests.test_library_scan import _seed_plugin, shared  # noqa: F401 — fixture
from viberpit import creds, fleet_role, mcp_oauth_refresh, paths
from viberpit.mcp_keys import cli_oauth_key
from viberpit.mcp_rekey import WantedServer

NEW_URL = "https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize"
OLD_URL = "https://aws-mcp.us-east-1.api.aws/mcp"
NEW_KEY = "aws-oauth|c6c1c83162f8ce2d"
OLD_KEY = "aws-oauth|751831b0c9c1bf84"
SECRETS = ("AT-live-secret", "RT-live-secret")


@pytest.fixture(autouse=True)
def _primary(monkeypatch):
    monkeypatch.delenv(fleet_role.ROLE_ENV, raising=False)


def _write_servers(servers: dict) -> None:
    creds.write_json_secure(paths.shared_claude_json_path(), {"mcpServers": servers})


def test_wanted_servers_http_sse_with_explicit_type_only(shared: Path):  # noqa: F811
    _write_servers({
        "aws-oauth": {"type": "http", "url": NEW_URL},
        "events": {"type": "sse", "url": "https://sse.example.com/s"},
        "guessed": {"url": "https://guess.example.com/mcp"},  # no explicit type: CLI key unknowable
        "local": {"type": "stdio", "command": "x"},
    })
    install = _seed_plugin(shared, "railway")
    (install / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"railway": {"type": "http", "url": "https://mcp.railway.com"}}}))
    wanted = {w.key: w for w in grants.wanted_servers()}
    assert set(wanted) == {NEW_KEY, "plugin:railway:railway|b12ecd269c942ba3",
                           cli_oauth_key("events", {"type": "sse", "url": "https://sse.example.com/s"})}
    assert wanted[NEW_KEY] == WantedServer(key=NEW_KEY, store_name="aws-oauth", url=NEW_URL)
    assert wanted["plugin:railway:railway|b12ecd269c942ba3"].store_name == "plugin:railway:railway"


def test_reconcile_grants_moves_and_reports(shared: Path, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL}})
    live = {"serverName": "aws-oauth", "serverUrl": OLD_URL, "accessToken": "AT-live-secret",
            "refreshToken": "RT-live-secret", "expiresAt": 9_999_999_999_999,
            "discoveryState": {"resourceMetadataUrl":
                               "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp"}}
    orphan = {"serverName": "linear", "serverUrl": "https://mcp.linear.app/mcp"}
    creds.write_json_secure(paths.shared_mcp_path(), {OLD_KEY: live, "linear|0": orphan})
    seen: list[str] = []

    def identity(prm_url, *, fetch_json=None):
        seen.append(prm_url)
        return ("https://auth.example.com", "https://aws-mcp.us-east-1.api.aws/mcp")

    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity", identity)
    drop = grants.reconcile_grants()
    assert drop == frozenset({OLD_KEY, "linear|0"})
    store = creds.read_shared_mcp()
    assert set(store) == {NEW_KEY}
    assert store[NEW_KEY]["refreshToken"] == "RT-live-secret"
    assert store[NEW_KEY]["serverUrl"] == NEW_URL
    assert seen  # the identity check really ran
    [line] = [r for r in caplog.records if "reconciled" in r.getMessage()]
    assert line.levelno == logging.INFO and OLD_KEY in line.getMessage()
    for rec in caplog.records:
        assert not any(s in rec.getMessage() for s in SECRETS)


def test_reconcile_grants_steady_state_is_quiet(shared: Path, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL}})
    creds.write_json_secure(paths.shared_mcp_path(), {NEW_KEY: {
        "serverName": "aws-oauth", "accessToken": "AT-live-secret", "refreshToken": "RT-live-secret"}})
    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity",
                        lambda *a, **k: pytest.fail("steady state must not fetch"))
    assert grants.reconcile_grants() == frozenset()
    assert not [r for r in caplog.records if "reconciled" in r.getMessage()]


def test_reconcile_grants_swallows_errors(shared: Path, monkeypatch, caplog):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scan, "list_mcp_servers", boom)
    with caplog.at_level(logging.WARNING):
        assert grants.reconcile_grants() == frozenset()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_edge_tick_reconciles_then_refreshes_with_the_drop_keys(monkeypatch):
    from server import app as app_mod

    calls: list = []
    monkeypatch.setattr(grants, "reconcile_grants", lambda: calls.append("reconcile") or frozenset({"old|k"}))
    monkeypatch.setattr(mcp_oauth_refresh, "run_once",
                        lambda **kw: calls.append(("run_once", kw)))
    await app_mod._mcp_oauth_tick()
    assert calls == ["reconcile", ("run_once", {"drop_keys": frozenset({"old|k"})})]
