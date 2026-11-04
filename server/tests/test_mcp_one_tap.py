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


@pytest.mark.parametrize("code", ["invalid_client", "INVALID_CLIENT"])
def test_invalid_client_forgets_registration_and_pastes_back(
        client: TestClient, fakes: Fakes, code: str) -> None:
    fakes.exchange_exc = ExchangeFailed(f"token endpoint returned 401 {code}", code=code)
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


def test_no_dcr_fallback_logs_no_warning(client: TestClient, fakes: Fakes,
                                         caplog: pytest.LogCaptureFixture) -> None:
    fakes.ensure_exc = RegistrationUnsupported("no dcr")
    with caplog.at_level("INFO", logger=one_tap.log.name):
        resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.headers["location"] == "/settings/mcp?pasteBack=railway"
    assert [r for r in caplog.records if r.levelno >= 30] == []
    assert any("RegistrationUnsupported" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [DiscoveryFailed("x"), RegistrationFailed("x")])
def test_real_start_failures_still_warn(client: TestClient, fakes: Fakes, exc: Exception,
                                        caplog: pytest.LogCaptureFixture) -> None:
    if isinstance(exc, DiscoveryFailed):
        fakes.discover_exc = exc
    else:
        fakes.ensure_exc = exc
    with caplog.at_level("INFO", logger=one_tap.log.name):
        client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert [r.levelno for r in caplog.records] == [30]


@pytest.mark.parametrize("code,level", [("invalid_grant", 20), ("INVALID_GRANT", 20),
                                        ("server_error", 30)])
def test_exchange_failure_log_level(client: TestClient, fakes: Fakes, code: str, level: int,
                                    caplog: pytest.LogCaptureFixture) -> None:
    fakes.exchange_exc = ExchangeFailed(f"token endpoint returned 400 {code}", code=code)
    state = _start(client)
    with caplog.at_level("INFO", logger=one_tap.log.name):
        assert _callback(client, code="c", state=state) == \
            "/settings/mcp?signInError=exchange_failed"
    assert [r.levelno for r in caplog.records] == [level]


def test_invalid_name_is_not_configured_even_without_public_origin(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(one_tap, "get_settings", lambda: types.SimpleNamespace(public_origin=None))
    for name in ("<script>", "nope"):
        resp = client.get("/api/mcp/oauth/start", params={"server": name})
        assert resp.headers["location"] == "/settings/mcp?signInError=not_configured"


def test_sign_in_failed_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError):
        one_tap.SignInFailed("access_denied: <script>")


@pytest.mark.parametrize("path", ["/api/mcp/oauth/start?server=railway",
                                  "/api/mcp/oauth/callback?code=c&state=s"])
def test_assistant_tokens_cannot_reach_one_tap_routes(path: str) -> None:
    from server.assistant.policy import route_allowed
    for method in ("GET", "POST"):
        assert route_allowed(method, path) is False
        assert route_allowed(method, path, operator=True) is False


def test_shutdown_reaps_follow_through_tasks() -> None:
    import asyncio

    async def run() -> bool:
        started = asyncio.Event()

        async def hang() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(hang())
        one_tap._background.add(task)
        await started.wait()
        await one_tap.shutdown()
        return task.cancelled() and not one_tap._background

    assert asyncio.run(run())


@pytest.mark.parametrize("error,expected", [("access_denied", "denied"),
                                            ("ACCESS_DENIED", "denied"),
                                            ("server_error", "exchange_failed"),
                                            ("invalid_scope", "exchange_failed")])
def test_provider_error_maps_to_a_closed_code(client: TestClient, fakes: Fakes, error: str,
                                              expected: str) -> None:
    state = _start(client)
    loc = _callback(client, error=error, state=state)
    assert loc == f"/settings/mcp?signInError={expected}"
    assert error not in loc and fakes.exchanges == [] and fakes.merges == []


def test_start_on_a_worker_is_not_configured(client: TestClient, fakes: Fakes,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from viberpit import fleet_role
    monkeypatch.setenv(fleet_role.ROLE_ENV, "worker")
    resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.headers["location"] == "/settings/mcp?signInError=not_configured"
    assert fakes.redirect_uris == [] and one_tap.pending_count() == 0


@pytest.mark.parametrize("where", ["ensure", "scan"])
def test_unexpected_start_failure_pastes_back(client: TestClient, fakes: Fakes, where: str,
                                              monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    if where == "ensure":  # e.g. the clients-cache write after a successful registration
        fakes.ensure_exc = OSError("disk full")
    else:
        def boom(status_rows=None):  # noqa: ANN001, ANN202
            raise RuntimeError("scan broke")
        monkeypatch.setattr(one_tap.scan, "list_mcp_servers", boom)
    with caplog.at_level("INFO"):
        resp = client.get("/api/mcp/oauth/start", params={"server": "railway"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/mcp?pasteBack=railway"
    warnings = [r for r in caplog.records if r.levelno == 30]
    assert len(warnings) == 1 and "disk full" not in warnings[0].getMessage()


def test_unexpected_start_failure_with_an_invalid_name_is_not_configured(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(identity, name):  # noqa: ANN001, ANN202
        raise RuntimeError("x")
    monkeypatch.setattr(one_tap, "start", boom)
    resp = client.get("/api/mcp/oauth/start", params={"server": "<script>"})
    assert resp.headers["location"] == "/settings/mcp?signInError=not_configured"


@pytest.mark.parametrize("where", ["forget", "exchange"])
def test_unexpected_callback_failure_is_exchange_failed(
        client: TestClient, fakes: Fakes, monkeypatch: pytest.MonkeyPatch, where: str,
        caplog: pytest.LogCaptureFixture) -> None:
    if where == "forget":
        fakes.exchange_exc = ExchangeFailed("401 invalid_client", code="invalid_client")

        def forget(issuer, **_kw):  # noqa: ANN001, ANN202
            raise OSError("read-only")
        monkeypatch.setattr(one_tap, "forget_client", forget)
    else:
        fakes.exchange_exc = RuntimeError("bug")
    state = _start(client)
    with caplog.at_level("INFO"):
        assert _callback(client, code="the-code", state=state) == \
            "/settings/mcp?signInError=exchange_failed"
    assert fakes.merges == []
    assert any(r.levelno == 30 and r.name == mcp_oauth.log.name for r in caplog.records)
    assert not any("the-code" in r.getMessage() for r in caplog.records
                   if r.name.startswith("server."))


def test_route_never_swallows_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def cancelled(*_a, **_kw):  # noqa: ANN202
        raise asyncio.CancelledError

    monkeypatch.setattr(one_tap, "start", cancelled)
    monkeypatch.setattr(one_tap, "complete", cancelled)
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mcp_oauth.oauth_start(server="railway", identity="t", db=None))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mcp_oauth.oauth_callback(request, code="c", state="s", error=None,  # type: ignore[arg-type]
                                             identity="t", db=None))


def _access_record(path: str, status: int = 302):  # noqa: ANN202
    import logging
    return logging.LogRecord("uvicorn.access", 20, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("100.64.0.1:5000", "GET", path, "1.1", status), None)


def test_access_filter_redacts_the_callback_query() -> None:
    f = mcp_oauth.CallbackAccessFilter()
    rec = _access_record("/api/mcp/oauth/callback?code=SECRET-CODE&state=SECRET-STATE")
    assert f.filter(rec) is True
    msg = rec.getMessage()
    assert "SECRET" not in msg and "/api/mcp/oauth/callback?<redacted>" in msg
    for path in ("/api/mcp/oauth/start?server=railway", "/api/mcp/oauth/callback",
                 "/api/sessions?x=1"):
        rec = _access_record(path)
        assert f.filter(rec) is True and path in rec.getMessage()
    odd = _access_record("/x")
    odd.args = ("only", "three", "args")
    assert f.filter(odd) is True


def test_access_filter_install_is_idempotent() -> None:
    import logging
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        mcp_oauth.install_callback_access_filter()
        mcp_oauth.install_callback_access_filter()
        assert sum(isinstance(f, mcp_oauth.CallbackAccessFilter) for f in access.filters) == 1
    finally:
        access.filters[:] = before
