"""One-tap MCP sign-in (MCP auth overhaul phase 4, task 3): start + callback routes.

Task 2's network functions are faked at ``server.mcp_health.one_tap``'s lookup site; the
shared MCP store is the real one under the conftest's isolated HOME."""
from __future__ import annotations

import types
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from viberpit import creds
from viberpit.mcp_oauth_client import (Client, Discovery, DiscoveryFailed, ExchangeFailed,
                                       RegistrationFailed, RegistrationUnsupported)

from server.api import mcp_oauth
from server.mcp_health import grants, one_tap

ORIGIN = "https://dobby.example.ts.net"
CALLBACK = f"{ORIGIN}/api/mcp/oauth/callback"
SERVER = {"name": "railway", "source": "user", "transport": "http",
          "config": {"type": "http", "url": "https://mcp.railway.example/mcp"}}
KEY = grants.key_for(SERVER)
DISCOVERY = Discovery(
    server_url="https://mcp.railway.example/mcp",
    prm_url="https://mcp.railway.example/.well-known/oauth-protected-resource/mcp",
    resource="https://mcp.railway.example/mcp",
    authorization_server="https://auth.railway.example/",
    as_metadata={"authorization_endpoint": "https://auth.railway.example/authorize",
                 "token_endpoint": "https://auth.railway.example/token"},
    as_metadata_url="https://auth.railway.example/.well-known/oauth-authorization-server",
    scopes_supported=("workspace:member",))
CLIENT = Client("cid-1", None, CALLBACK)


class Fakes:
    def __init__(self) -> None:
        self.discover_exc: Exception | None = None
        self.ensure_exc: Exception | None = None
        self.exchange_exc: Exception | None = None
        self.token: dict = {"accessToken": "at-1", "refreshToken": "rt-1",
                            "expiresAt": 9_999_999_999_999}
        self.exchanges: list[dict] = []
        self.forgotten: list[str] = []
        self.redirect_uris: list[str] = []
        self.merges: list[tuple[dict, set | None]] = []
        self.after_login: list[tuple] = []
        self.audits: list[tuple[str, dict]] = []


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()
    one_tap.reset_pending()
    creds.update_shared_mcp(lambda store: {k: v for k, v in store.items() if k != KEY})

    def discover(server_url, **_kw):  # noqa: ANN001, ANN202
        if f.discover_exc:
            raise f.discover_exc
        assert server_url == SERVER["config"]["url"]
        return DISCOVERY

    def ensure_client(discovery, redirect_uri, **_kw):  # noqa: ANN001, ANN202
        f.redirect_uris.append(redirect_uri)
        if f.ensure_exc:
            raise f.ensure_exc
        return CLIENT

    def exchange_code(discovery, client, *, code, verifier, server_name, now_ms, **_kw):  # noqa: ANN001, ANN202
        f.exchanges.append({"code": code, "verifier": verifier, "server_name": server_name})
        if f.exchange_exc:
            raise f.exchange_exc
        return {"serverName": server_name, "serverUrl": discovery.server_url, **f.token,
                "clientId": client.client_id, "redirectUri": client.redirect_uri,
                "discoveryState": {"authorizationServerUrl": discovery.authorization_server,
                                   "resourceMetadataUrl": discovery.prm_url,
                                   "oauthMetadataFound": True}}

    def forget_client(issuer, **_kw):  # noqa: ANN001, ANN202
        f.forgotten.append(issuer)

    real_merge = creds.merge_shared_mcp

    def merge(incoming, *, authenticated_keys=None):  # noqa: ANN001, ANN202
        f.merges.append((incoming, authenticated_keys))
        return real_merge(incoming, authenticated_keys=authenticated_keys)

    async def _noop() -> None:
        return None

    def after_login(orch, server, accounts, **kw):  # noqa: ANN001, ANN202
        f.after_login.append((orch, server, list(accounts), kw))
        return _noop()

    async def audit(*, db, type, identity, detail, **_kw):  # noqa: ANN001, A002, ANN202
        f.audits.append((type, detail))

    monkeypatch.setattr(one_tap, "discover", discover)
    monkeypatch.setattr(one_tap, "ensure_client", ensure_client)
    monkeypatch.setattr(one_tap, "exchange_code", exchange_code)
    monkeypatch.setattr(one_tap, "forget_client", forget_client)
    monkeypatch.setattr(one_tap.creds, "merge_shared_mcp", merge)
    monkeypatch.setattr(one_tap, "after_login", after_login)
    monkeypatch.setattr(one_tap, "_account_names", lambda: ["alice", "bob"])
    monkeypatch.setattr(one_tap.scan, "list_mcp_servers",
                        lambda status_rows=None: {"servers": [SERVER]})
    monkeypatch.setattr(one_tap, "get_settings",
                        lambda: types.SimpleNamespace(public_origin=ORIGIN))
    monkeypatch.setattr(mcp_oauth, "record_audit", audit)
    return f


IDENTITY = {"who": "tony@example.com"}


@pytest.fixture
def client(fakes: Fakes) -> TestClient:
    from server.auth.middleware import current_identity
    from server.db import get_db

    app = FastAPI()
    app.include_router(mcp_oauth.router)
    app.state.sessions = "ORCH"
    IDENTITY["who"] = "tony@example.com"
    app.dependency_overrides[current_identity] = lambda: IDENTITY["who"]
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, follow_redirects=False)


def _start(client: TestClient, name: str = "railway") -> str:
    """Start a sign-in; return the state the provider would echo back."""
    resp = client.get("/api/mcp/oauth/start", params={"server": name})
    assert resp.status_code == 302, resp.text
    loc = resp.headers["location"]
    assert loc.startswith("https://auth.railway.example/authorize?")
    return urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)["state"][0]


def _callback(client: TestClient, **params: str) -> str:
    resp = client.get("/api/mcp/oauth/callback", params=params)
    assert resp.status_code == 302, resp.text
    return resp.headers["location"]


def test_start_redirects_to_the_provider_with_pkce(client: TestClient, fakes: Fakes) -> None:
    resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.status_code == 302
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [CALLBACK]
    assert query["client_id"] == ["cid-1"]
    assert query["resource"] == ["https://mcp.railway.example/mcp"]
    assert len(query["state"][0]) >= 43  # 32 random bytes, url-safe
    assert fakes.redirect_uris == [CALLBACK]
    assert fakes.audits == [("library_mcp_auth_started", {"name": "railway"})]


def test_callback_happy_path_writes_bundle_at_cli_key(client: TestClient, fakes: Fakes) -> None:
    state = _start(client)
    loc = _callback(client, code="the-code", state=state)
    assert loc == "/settings/mcp?signedIn=railway"
    assert fakes.exchanges[0]["code"] == "the-code"
    assert len(fakes.exchanges[0]["verifier"]) == 64
    assert fakes.exchanges[0]["server_name"] == "railway"
    assert len(fakes.merges) == 1
    incoming, authenticated = fakes.merges[0]
    assert set(incoming) == {KEY} and authenticated == {KEY}
    stored = creds.read_shared_mcp()[KEY]
    assert stored["accessToken"] == "at-1" and stored["clientId"] == "cid-1"
    assert fakes.after_login == [("ORCH", "railway", ["alice", "bob"], {"signed_in": True})]
    assert ("library_mcp_auth_completed", {"name": "railway"}) in fakes.audits


def test_callback_rejects_foreign_or_replayed_state(client: TestClient, fakes: Fakes) -> None:
    state = _start(client)
    IDENTITY["who"] = "mallory@example.com"
    assert _callback(client, code="c", state=state) == "/settings/mcp?signInError=state_invalid"
    # Single use: even the right identity cannot use it afterwards.
    IDENTITY["who"] = "tony@example.com"
    assert _callback(client, code="c", state=state) == "/settings/mcp?signInError=state_invalid"

    state2 = _start(client)
    assert _callback(client, code="c", state=state2) == "/settings/mcp?signedIn=railway"
    assert _callback(client, code="c", state=state2) == "/settings/mcp?signInError=state_invalid"
    assert len(fakes.exchanges) == 1 and len(fakes.merges) == 1
    assert _callback(client, code="c", state="never-issued") == \
        "/settings/mcp?signInError=state_invalid"
    assert _callback(client, code="c") == "/settings/mcp?signInError=state_invalid"
    assert len(fakes.merges) == 1


def test_foreign_state_writes_nothing(client: TestClient, fakes: Fakes) -> None:
    state = _start(client)
    IDENTITY["who"] = "mallory@example.com"
    _callback(client, code="c", state=state)
    assert fakes.exchanges == [] and fakes.merges == [] and fakes.after_login == []
    assert KEY not in creds.read_shared_mcp()


@pytest.mark.parametrize("exc", [RegistrationUnsupported("no dcr"), RegistrationFailed("400"),
                                 DiscoveryFailed("none")])
def test_start_falls_back_to_paste_back_on_registration_failure(
        client: TestClient, fakes: Fakes, exc: Exception) -> None:
    if isinstance(exc, DiscoveryFailed):
        fakes.discover_exc = exc
    else:
        fakes.ensure_exc = exc
    resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/mcp?pasteBack=railway"


def test_start_without_public_origin_uses_paste_back(
        client: TestClient, fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(one_tap, "get_settings", lambda: types.SimpleNamespace(public_origin=None))
    resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/mcp?pasteBack=railway"
    assert fakes.redirect_uris == []
    # And a stray callback does not 500.
    assert _callback(client, code="c", state="s") == "/settings/mcp?signInError=state_invalid"


@pytest.mark.parametrize("servers,name", [
    ([SERVER], "nope"),
    ([SERVER, {**SERVER, "source": "plugin@x", "pluginName": "x"}], "railway"),
    ([{"name": "local", "transport": "stdio", "config": {"command": "x"}}], "local"),
    ([SERVER], "bad name/../"),
    ([SERVER], ""),
])
def test_start_unknown_server_is_not_configured(
        client: TestClient, monkeypatch: pytest.MonkeyPatch, servers: list, name: str) -> None:
    monkeypatch.setattr(one_tap.scan, "list_mcp_servers",
                        lambda status_rows=None: {"servers": servers})
    resp = client.get("/api/mcp/oauth/start", params={"server": name})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/mcp?signInError=not_configured"


def test_start_url_quotes_the_name(client: TestClient, fakes: Fakes,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    server = {**SERVER, "name": "my.srv_1"}
    monkeypatch.setattr(one_tap.scan, "list_mcp_servers",
                        lambda status_rows=None: {"servers": [server]})
    fakes.ensure_exc = RegistrationUnsupported("x")
    resp = client.get("/api/mcp/oauth/start", params={"server": "my.srv_1"})
    assert resp.headers["location"] == "/settings/mcp?pasteBack=my.srv_1"


def test_callback_handles_access_only_token(client: TestClient, fakes: Fakes) -> None:
    fakes.token = {"accessToken": "at-only"}
    state = _start(client)
    assert _callback(client, code="c", state=state) == "/settings/mcp?signedIn=railway"
    stored = creds.read_shared_mcp()[KEY]
    assert stored["accessToken"] == "at-only"
    assert "expiresAt" not in stored and "refreshToken" not in stored


def test_provider_error_is_denied_and_never_reflected(client: TestClient, fakes: Fakes) -> None:
    state = _start(client)
    resp = client.get("/api/mcp/oauth/callback", params={
        "error": "access_denied", "error_description": "<script>alert(1)</script>",
        "state": state})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/mcp?signInError=denied"
    assert "script" not in resp.text and "access_denied" not in resp.text
    assert "script" not in resp.headers["location"]
    assert fakes.exchanges == [] and fakes.merges == []
    # The state was consumed.
    assert _callback(client, code="c", state=state) == "/settings/mcp?signInError=state_invalid"


def test_invalid_client_forgets_registration_and_pastes_back(
        client: TestClient, fakes: Fakes) -> None:
    fakes.exchange_exc = ExchangeFailed("token endpoint returned 401 invalid_client",
                                        code="invalid_client")
    state = _start(client)
    assert _callback(client, code="c", state=state) == "/settings/mcp?pasteBack=railway"
    assert fakes.forgotten == [DISCOVERY.authorization_server]
    assert fakes.merges == []


def test_other_exchange_failure_is_exchange_failed(client: TestClient, fakes: Fakes) -> None:
    fakes.exchange_exc = ExchangeFailed("token endpoint returned 400 invalid_grant",
                                        code="invalid_grant")
    state = _start(client)
    assert _callback(client, code="c", state=state) == "/settings/mcp?signInError=exchange_failed"
    assert fakes.forgotten == [] and fakes.merges == [] and fakes.after_login == []


def test_state_expires_after_ttl(client: TestClient, fakes: Fakes,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(one_tap, "_clock", lambda: now[0])
    state = _start(client)
    now[0] += one_tap.STATE_TTL_S + 1
    assert _callback(client, code="c", state=state) == "/settings/mcp?signInError=state_invalid"
    assert fakes.exchanges == []
    assert one_tap.STATE_TTL_S == 600


def test_pending_table_is_bounded(client: TestClient, fakes: Fakes) -> None:
    for _ in range(one_tap.MAX_PENDING + 5):
        _start(client)
    assert one_tap.pending_count() == one_tap.MAX_PENDING
