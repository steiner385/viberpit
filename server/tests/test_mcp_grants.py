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
    grants._tombstones.clear()
    yield
    grants._tombstones.clear()


LIVE = {"serverName": "aws-oauth", "serverUrl": OLD_URL, "accessToken": "AT-live-secret",
        "refreshToken": "RT-live-secret", "expiresAt": 9_999_999_999_999,
        "discoveryState": {"resourceMetadataUrl":
                           "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp"}}
DISCOVERY = {"serverName": "aws-oauth", "serverUrl": NEW_URL, "accessToken": "", "expiresAt": 0}
AWS_ID = ("https://auth.example.com", "https://aws-mcp.us-east-1.api.aws/mcp")


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
    # An empty bundle of a plugin that is not enabled is dropped; a plain unconfigured
    # name (a project .mcp.json server) is kept.
    orphan = {"serverName": "plugin:linear:linear", "serverUrl": "https://mcp.linear.app/mcp"}
    projsrv = {"serverName": "projsrv", "serverUrl": "https://proj.example/mcp"}
    creds.write_json_secure(paths.shared_mcp_path(), {OLD_KEY: LIVE, NEW_KEY: DISCOVERY,
                                                      "plugin:linear:linear|0": orphan,
                                                      "projsrv|0": projsrv})
    seen: list[str] = []

    def identity(prm_url, *, fetch_json=None):
        seen.append(prm_url)
        return AWS_ID

    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity", identity)
    drop = grants.reconcile_grants().drop
    assert drop == frozenset({OLD_KEY, "plugin:linear:linear|0"})
    store = creds.read_shared_mcp()
    assert set(store) == {NEW_KEY, "projsrv|0"}
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
    assert grants.reconcile_grants().drop == frozenset()
    assert not [r for r in caplog.records if "reconciled" in r.getMessage()]


def test_reconcile_grants_swallows_errors(shared: Path, monkeypatch, caplog):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scan, "list_mcp_servers", boom)
    with caplog.at_level(logging.WARNING):
        assert grants.reconcile_grants().drop == frozenset()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_edge_tick_reconciles_then_refreshes_with_the_drop_keys(monkeypatch):
    from server import app as app_mod

    calls: list = []
    monkeypatch.setattr(grants, "reconcile_grants", lambda: calls.append("reconcile") or grants.ReconcileResult(
                            drop=frozenset({"old|k"}), skip=frozenset({"sup|1"})))
    monkeypatch.setattr(mcp_oauth_refresh, "run_once",
                        lambda **kw: calls.append(("run_once", kw)))
    await app_mod._mcp_oauth_tick()
    assert calls == ["reconcile", ("run_once", {"drop_keys": frozenset({"old|k"}),
                                          "skip_keys": frozenset({"sup|1"})})]


def test_key_for_skips_placeholders_and_non_candidates():
    base = {"name": "aws-oauth", "transport": "http"}
    assert grants.key_for({**base, "config": {"type": "http", "url": NEW_URL}}) == NEW_KEY
    assert grants.key_for({**base, "config": {"type": "http", "url": "https://${HOST}/mcp"}}) is None
    assert grants.key_for({**base, "config": {"type": "http", "url": NEW_URL,
                                              "headers": {"Authorization": "Bearer ${TOKEN}"}}}) is None
    assert grants.key_for({**base, "config": {"type": "http"}}) is None
    assert grants.key_for({**base, "config": {"url": NEW_URL}}) is None  # no explicit type
    assert grants.key_for({"name": "x", "transport": "stdio", "config": {"type": "stdio"}}) is None


def test_wanted_servers_exclude_placeholder_configs(shared: Path):  # noqa: F811
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL},
                    "env-url": {"type": "http", "url": "https://${HOST}/mcp"},
                    "env-hdr": {"type": "http", "url": "https://h.example/mcp",
                                "headers": {"X-Key": "${KEY}"}}})
    assert [w.key for w in grants.wanted_servers()] == [NEW_KEY]


def test_identity_fetched_outside_the_store_lock(shared: Path, monkeypatch):  # noqa: F811
    """Identities are prefetched from a snapshot before the lock; the locked plan only looks
    them up. A donor that appears only under the lock is unknown, so nothing moves."""
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL}})
    creds.write_json_secure(paths.shared_mcp_path(), {NEW_KEY: DISCOVERY})
    in_lock = [False]
    fetched: list[tuple[str, bool]] = []

    def identity(prm_url, *, fetch_json=None):
        # Recorded, not asserted: reconcile swallows exceptions, so an assert here would hide.
        fetched.append((prm_url, in_lock[0]))
        return AWS_ID

    real_update = creds.update_shared_mcp

    def update(fn):
        in_lock[0] = True
        try:
            creds.write_json_secure(paths.shared_mcp_path(), {NEW_KEY: DISCOVERY, OLD_KEY: LIVE})
            return real_update(fn)
        finally:
            in_lock[0] = False

    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity", identity)
    monkeypatch.setattr(creds, "update_shared_mcp", update)
    assert grants.reconcile_grants().drop == frozenset()
    assert fetched == []
    assert set(creds.read_shared_mcp()) == {NEW_KEY, OLD_KEY}  # nothing moved


def test_identity_prefetch_feeds_the_locked_plan(shared: Path, monkeypatch):  # noqa: F811
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL}})
    creds.write_json_secure(paths.shared_mcp_path(), {NEW_KEY: DISCOVERY, OLD_KEY: LIVE})
    in_lock = [False]
    fetched: list[tuple[str, bool]] = []

    def identity(prm_url, *, fetch_json=None):
        # Recorded, not asserted: reconcile swallows exceptions, so an assert here would hide.
        fetched.append((prm_url, in_lock[0]))
        return AWS_ID

    real_update = creds.update_shared_mcp

    def update(fn):
        in_lock[0] = True
        try:
            return real_update(fn)
        finally:
            in_lock[0] = False

    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity", identity)
    monkeypatch.setattr(creds, "update_shared_mcp", update)
    assert grants.reconcile_grants().drop == frozenset({OLD_KEY})
    assert fetched and not any(locked for _, locked in fetched)  # prefetched before the lock
    assert creds.read_shared_mcp()[NEW_KEY]["refreshToken"] == "RT-live-secret"


def test_purged_keys_tombstoned_for_thirty_minutes(shared: Path, monkeypatch):  # noqa: F811
    _write_servers({})
    dead = {"serverName": "plugin:linear:linear", "serverUrl": "https://mcp.linear.app/mcp"}
    creds.write_json_secure(paths.shared_mcp_path(), {"plugin:linear:linear|0": dead})
    t0 = 5000.0
    assert grants.reconcile_grants(clock=lambda: t0).drop == frozenset({"plugin:linear:linear|0"})
    assert creds.read_shared_mcp() == {}
    assert grants.reconcile_grants(clock=lambda: t0 + 10 * 60).drop == frozenset({"plugin:linear:linear|0"})
    assert grants.reconcile_grants(clock=lambda: t0 + 31 * 60).drop == frozenset()


def test_tombstone_not_returned_once_the_key_is_back_in_the_store(shared: Path):  # noqa: F811
    _write_servers({})
    grants._tombstones["back|1"] = 10_000.0
    creds.write_json_secure(paths.shared_mcp_path(), {"back|1": {
        "serverName": "back", "accessToken": "a", "refreshToken": "r"}})
    assert grants.reconcile_grants(clock=lambda: 100.0).drop == frozenset()


def test_unreadable_user_config_skips_the_tick(shared: Path, monkeypatch, caplog):  # noqa: F811
    paths.shared_claude_json_path().parent.mkdir(parents=True, exist_ok=True)
    paths.shared_claude_json_path().write_text("{not json")
    store = {OLD_KEY: {"serverName": "aws-oauth", "accessToken": "", "expiresAt": 0}}
    creds.write_json_secure(paths.shared_mcp_path(), store)
    grants._tombstones["gone|1"] = 10_000.0
    monkeypatch.setattr(creds, "update_shared_mcp", lambda fn: pytest.fail("must not write"))
    with caplog.at_level(logging.WARNING):
        assert grants.reconcile_grants(clock=lambda: 100.0).drop == frozenset({"gone|1"})
    assert creds.read_shared_mcp() == store
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_absent_user_config_is_no_user_servers(shared: Path):  # noqa: F811
    assert not paths.shared_claude_json_path().exists()
    dead = {"serverName": "plugin:linear:linear", "serverUrl": "https://mcp.linear.app/mcp"}
    creds.write_json_secure(paths.shared_mcp_path(), {"plugin:linear:linear|0": dead})
    assert grants.reconcile_grants().drop == frozenset({"plugin:linear:linear|0"})


def _w(key):
    return WantedServer(key=key, store_name="n", url="https://x.example/mcp")


def test_superseded_refresh_keys():
    def rt(name, **kw):
        return {"serverName": name, "refreshToken": "r", **kw}

    wanted = [WantedServer(key="wanted|1", store_name="srv", url="https://x.example/mcp")]
    store = {"wanted|1": rt("srv"), "stale|1": rt("srv"),
             "stale-empty|1": {"serverName": "srv"}, "stale-blank|1": rt("srv", refreshToken=""),
             "unconfigured|1": rt("projsrv"), "junk|1": "not-a-dict"}
    assert grants.superseded_refresh_keys(store, wanted) == frozenset({"stale|1"})
    assert grants.superseded_refresh_keys(store, wanted + [_w("stale|1")]) == frozenset()


def test_superseded_refresh_keys_empty_wanted_skips_nothing():
    store = {"a|1": {"serverName": "a", "refreshToken": "r"}, "b|1": {"serverName": "b", "refreshToken": "r"}}
    assert grants.superseded_refresh_keys(store, []) == frozenset()


def test_reconcile_skips_old_key_of_configured_server_not_unconfigured(shared: Path, monkeypatch):  # noqa: F811
    _write_servers({"aws-oauth": {"type": "http", "url": NEW_URL}})
    creds.write_json_secure(paths.shared_mcp_path(), {
        NEW_KEY: {"serverName": "aws-oauth", "refreshToken": "RT-live-secret"},
        OLD_KEY: {"serverName": "aws-oauth", "refreshToken": "RT-old"},
        "projsrv|0": {"serverName": "projsrv", "refreshToken": "RT-proj"}})
    monkeypatch.setattr(mcp_oauth_refresh, "grant_identity", lambda *a, **k: None)
    result = grants.reconcile_grants()
    assert result.skip == frozenset({OLD_KEY})
    assert "projsrv|0" not in result.skip


def test_reconcile_error_paths_skip_nothing(shared: Path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(scan, "list_mcp_servers", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert grants.reconcile_grants() == grants.ReconcileResult(drop=frozenset(), skip=frozenset())
