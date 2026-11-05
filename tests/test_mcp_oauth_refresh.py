"""The single MCP OAuth refresher (spec 2026-10-13): fake HTTP, isolated store."""
import base64
import json
import logging
import urllib.parse

import pytest

from viberpit import creds, mcp_oauth_refresh as mor, paths

NOW = 1_000_000_000_000
SERVER_URL = "https://mcp.example.com/mcp"
AS_URL = "https://auth.example.com/tenant/"
TOKEN_URL = "https://auth.example.com/oauth2/token"
SECRETS = ("AT-OLD-secret", "RT-OLD-secret", "AT-NEW-secret", "RT-NEW-secret", "CLIENT-SECRET-x")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    mor._metadata_cache.clear()
    mor._backoff_until_ms.clear()
    mor._resource_cache.clear()
    mor._lifetime_s.clear()
    yield
    mor._metadata_cache.clear()
    mor._backoff_until_ms.clear()
    mor._resource_cache.clear()
    mor._lifetime_s.clear()


def _bundle(**over):
    b = {"serverName": "aws-oauth", "serverUrl": SERVER_URL, "clientId": "cid",
         "accessToken": "AT-OLD-secret", "refreshToken": "RT-OLD-secret", "expiresAt": NOW + 60_000,
         "discoveryState": {"authorizationServerUrl": AS_URL}}
    b.update(over)
    return b


class FakeHttp:
    def __init__(self, *, metadata=None, responses=None):
        self.metadata = metadata or {}
        self.responses = list(responses or [])
        self.fetched: list[str] = []
        self.posts: list[tuple[str, dict, dict]] = []

    def fetch_json(self, url):
        self.fetched.append(url)
        return self.metadata.get(url)

    def post_form(self, url, form, headers):
        self.posts.append((url, dict(form), dict(headers)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body


def _ok(**over):
    body = {"access_token": "AT-NEW-secret", "refresh_token": "RT-NEW-secret", "expires_in": 3600,
            "token_type": "Bearer"}
    body.update(over)
    return (200, {k: v for k, v in body.items() if v is not None})


# --- due_keys / cleared ---------------------------------------------------------------------

def test_due_keys_selects_refreshable_bundles_inside_the_lead():
    store = {
        "due": _bundle(expiresAt=NOW + 599_000),
        "expired": _bundle(expiresAt=NOW - 1),
        "later": _bundle(expiresAt=NOW + 601_000),
        "no_rt": _bundle(refreshToken="", expiresAt=NOW),
        "no_exp": {k: v for k, v in _bundle().items() if k != "expiresAt"},
        "null_exp": _bundle(expiresAt=None),
        "bool_exp": _bundle(expiresAt=True),
        "junk": None,
    }
    assert sorted(mor.due_keys(store, NOW)) == ["due", "expired"]
    assert sorted(mor.due_keys(store, NOW, lead_s=700)) == ["due", "expired", "later"]


def test_cleared_matches_the_cli_shape():
    out = mor.cleared(_bundle())
    assert out["accessToken"] == "" and out["expiresAt"] == 0 and "refreshToken" not in out
    assert out["clientId"] == "cid" and out["serverUrl"] == SERVER_URL


def test_cleared_keys_are_the_cleared_shape():
    store = {"dead": mor.cleared(_bundle()), "live": _bundle(),
             "at_only": {**mor.cleared(_bundle()), "accessToken": "AT"},
             "rt_only": {**mor.cleared(_bundle()), "refreshToken": "RT"},
             "stub": {"serverName": "x"}, "junk": None}
    assert mor.cleared_keys(store) == frozenset({"dead", "stub"})


# --- token endpoint discovery ---------------------------------------------------------------

def test_explicit_metadata_url_wins():
    http = FakeHttp(metadata={"https://meta/x": {"token_endpoint": TOKEN_URL}})
    b = _bundle(discoveryState={"authServerMetadataUrl": "https://meta/x", "authorizationServerUrl": AS_URL})
    assert mor.token_endpoint(b, fetch_json=http.fetch_json) == TOKEN_URL
    assert http.fetched == ["https://meta/x"]


def test_discovery_order_with_path_insertion():
    http = FakeHttp()
    assert mor.token_endpoint(_bundle(), fetch_json=http.fetch_json) == "https://auth.example.com/token"
    assert http.fetched == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
        "https://auth.example.com/.well-known/openid-configuration/tenant",
        "https://auth.example.com/tenant/.well-known/openid-configuration",
    ]


def test_discovery_root_issuer_and_oidc_hit():
    oidc = "https://auth.example.com/.well-known/openid-configuration"
    http = FakeHttp(metadata={oidc: {"token_endpoint": TOKEN_URL}})
    b = _bundle(discoveryState={"authorizationServerUrl": "https://auth.example.com/"})
    assert mor.token_endpoint(b, fetch_json=http.fetch_json) == TOKEN_URL
    assert http.fetched == ["https://auth.example.com/.well-known/oauth-authorization-server", oidc]


def test_discovery_falls_back_to_issuer_then_server_origin():
    http = FakeHttp()
    b = _bundle(discoveryState={}, issuer="https://iss.example.com")
    assert mor.token_endpoint(b, fetch_json=http.fetch_json) == "https://iss.example.com/token"
    b = _bundle(discoveryState=None)
    assert mor.token_endpoint(b, fetch_json=http.fetch_json) == "https://mcp.example.com/token"


def test_discovery_is_memoized_per_auth_server():
    url = "https://auth.example.com/.well-known/oauth-authorization-server/tenant"
    http = FakeHttp(metadata={url: {"token_endpoint": TOKEN_URL}})
    mor.token_endpoint(_bundle(), fetch_json=http.fetch_json)
    mor.token_endpoint(_bundle(), fetch_json=http.fetch_json)
    assert http.fetched == [url]


def test_discovery_errors_fall_through():
    def boom(url):
        raise OSError("down")
    assert mor.token_endpoint(_bundle(), fetch_json=boom) == "https://auth.example.com/token"


# --- refresh_bundle -------------------------------------------------------------------------

def _meta(**extra):
    url = "https://auth.example.com/.well-known/oauth-authorization-server/tenant"
    return {url: {"token_endpoint": TOKEN_URL, **extra}}


def test_refresh_success_rotates_and_sends_resource():
    http = FakeHttp(metadata=_meta(), responses=[_ok(scope="read")])
    new = mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert new["accessToken"] == "AT-NEW-secret" and new["refreshToken"] == "RT-NEW-secret"
    assert new["expiresAt"] == NOW + 3_600_000 and new["scope"] == "read"
    assert new["clientId"] == "cid" and new["serverName"] == "aws-oauth"
    url, form, headers = http.posts[0]
    assert url == TOKEN_URL
    assert form == {"grant_type": "refresh_token", "refresh_token": "RT-OLD-secret", "client_id": "cid",
                    "resource": SERVER_URL}
    assert "Authorization" not in headers


def test_refresh_uses_the_configured_client_id_when_the_bundle_has_none(tmp_path):
    # A pre-registered client (KinDash refuses dynamic registration) whose bundle never
    # recorded its id must still send client_id, or the token endpoint answers 400.
    from viberpit import paths
    shared = paths.shared_claude_json_path()
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"mcpServers": {"aws-oauth": {"oauth": {"clientId": "pinned-cid"}}}}))
    bundle = _bundle()
    del bundle["clientId"]
    http = FakeHttp(metadata=_meta(), responses=[_ok()])
    new = mor.refresh_bundle(bundle, post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    _, form, _ = http.posts[0]
    assert form["client_id"] == "pinned-cid"
    assert new["clientId"] == "pinned-cid"


def test_refresh_keeps_old_refresh_token_when_omitted_and_drops_expiry_without_expires_in():
    http = FakeHttp(metadata=_meta(), responses=[_ok(refresh_token=None, expires_in=None)])
    new = mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert new["refreshToken"] == "RT-OLD-secret"
    assert "expiresAt" not in new


def test_client_secret_basic_by_default():
    http = FakeHttp(metadata=_meta(), responses=[_ok()])
    mor.refresh_bundle(_bundle(clientSecret="CLIENT-SECRET-x"), post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    _, form, headers = http.posts[0]
    assert "client_secret" not in form
    raw = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert raw == "cid:" + urllib.parse.quote("CLIENT-SECRET-x", safe="")


def test_client_secret_post_when_basic_unsupported():
    http = FakeHttp(metadata=_meta(token_endpoint_auth_methods_supported=["client_secret_post"]),
                    responses=[_ok()])
    mor.refresh_bundle(_bundle(clientSecret="CLIENT-SECRET-x"), post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    _, form, headers = http.posts[0]
    assert form["client_secret"] == "CLIENT-SECRET-x" and "Authorization" not in headers


def test_refresh_authenticates_the_way_the_exchange_did():
    # The server lists basic too (the default pick), but the client was registered — and
    # the code exchanged — as client_secret_post; the bundle records that.
    http = FakeHttp(metadata=_meta(token_endpoint_auth_methods_supported=["client_secret_basic",
                                                                          "client_secret_post"]),
                    responses=[_ok()])
    new = mor.refresh_bundle(_bundle(clientSecret="CLIENT-SECRET-x",
                                     tokenEndpointAuthMethod="client_secret_post"),
                             post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    _, form, headers = http.posts[0]
    assert form["client_secret"] == "CLIENT-SECRET-x" and "Authorization" not in headers
    assert new["tokenEndpointAuthMethod"] == "client_secret_post"


def test_refresh_falls_back_to_the_cached_registrations_auth_method():
    # A bundle from before the exchange recorded the method: the client cache for its
    # authorization server knows it — but only for that very client.
    def register(cache):
        cache[mor.url_href(AS_URL)] = {"client_id": "cid", "redirect_uri": "r",
                                       "token_endpoint_auth_method": "client_secret_post"}
        return cache
    creds.update_mcp_clients(register)
    http = FakeHttp(metadata=_meta(), responses=[_ok(), _ok()])
    mor.refresh_bundle(_bundle(clientSecret="CLIENT-SECRET-x"), post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    _, form, headers = http.posts[0]
    assert form["client_secret"] == "CLIENT-SECRET-x" and "Authorization" not in headers
    mor.refresh_bundle(_bundle(clientId="cid-other", clientSecret="CLIENT-SECRET-x"),
                       post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    _, form, headers = http.posts[1]
    assert "client_secret" not in form and headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize("status,code", [(400, "invalid_grant"), (401, "invalid_grant"),
                                         (400, "invalid_refresh_token"), (400, "expired_refresh_token"),
                                         (401, "token_expired")])
def test_dead_grant_classes(status, code):
    http = FakeHttp(metadata=_meta(), responses=[(status, {"error": code})])
    with pytest.raises(mor.McpGrantDead):
        mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)


@pytest.mark.parametrize("response", [
    (400, {"error": "invalid_target"}), (400, {"error": "invalid_request"}), (401, {"error": "invalid_client"}),
    (401, b"not json"), (403, {"error": "invalid_grant"}), (429, {}), (500, {"error": "invalid_grant"}),
    (200, {"token_type": "Bearer"}), OSError("connection reset"),
])
def test_transient_classes(response):
    http = FakeHttp(metadata=_meta(), responses=[response])
    with pytest.raises(mor.McpRefreshTransient):
        mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)


def test_error_classes_are_siblings():
    assert not issubclass(mor.McpRefreshTransient, mor.McpGrantDead)
    assert not issubclass(mor.McpGrantDead, mor.McpRefreshTransient)


# --- run_once -------------------------------------------------------------------------------

def _seed(store, dirs):
    creds.write_json_secure(paths.shared_mcp_path(), store)
    for name, mcp in dirs.items():
        creds.write_json_secure(paths.account_cfg_dir(name) / ".credentials.json",
                                {"claudeAiOauth": {"accessToken": f"claude-{name}"}, "mcpOAuth": mcp})


def _dir(name):
    return json.loads((paths.account_cfg_dir(name) / ".credentials.json").read_text())


def _assert_no_secret_logged(caplog):
    for rec in caplog.records:
        text = rec.getMessage() + (str(rec.exc_info) if rec.exc_info else "")
        for s in SECRETS:
            assert s not in text, f"{s!r} leaked in log: {text}"


def test_run_once_refreshes_and_publishes_to_every_account(caplog):
    caplog.set_level(logging.DEBUG)
    legacy = _bundle()  # every dir still carries the shared refresh token (pre-fix layout)
    _seed({"aws|1": legacy, "other": {"serverName": "o", "accessToken": "x"}},
          {"alice": {"aws|1": legacy}, "bob": {"aws|1": legacy}})
    http = FakeHttp(metadata=_meta(), responses=[_ok()])
    out = mor.run_once(accounts_list=["alice", "bob"], post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    assert out["refreshed"] == ["aws-oauth"] and out["dead"] == [] and out["transient"] == []
    assert out["published"] >= 2
    assert len(http.posts) == 1  # one refresher, one grant use
    store = creds.read_json(paths.shared_mcp_path())
    assert store["aws|1"]["refreshToken"] == "RT-NEW-secret"
    assert store["aws|1"]["expiresAt"] == NOW + 3_600_000
    for name in ("alice", "bob"):
        d = _dir(name)
        assert d["claudeAiOauth"] == {"accessToken": f"claude-{name}"}
        assert d["mcpOAuth"]["aws|1"]["accessToken"] == "AT-NEW-secret"
        assert d["mcpOAuth"]["other"] == {"serverName": "o", "accessToken": "x"}
        assert all("refreshToken" not in b for b in d["mcpOAuth"].values())
    assert any("aws-oauth" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    _assert_no_secret_logged(caplog)


def test_run_once_dead_grant_clears_store_and_never_retries(caplog):
    caplog.set_level(logging.DEBUG)
    _seed({"aws|1": _bundle(expiresAt=NOW - 5)}, {"alice": {}})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_grant"})])
    out = mor.run_once(accounts_list=["alice"], post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    assert out["dead"] == ["aws-oauth"]
    store = creds.read_json(paths.shared_mcp_path())
    assert store["aws|1"] == mor.cleared(_bundle(expiresAt=NOW - 5))
    assert _dir("alice")["mcpOAuth"]["aws|1"]["accessToken"] in ("", "AT-OLD-secret")
    assert "refreshToken" not in _dir("alice")["mcpOAuth"]["aws|1"]
    again = mor.run_once(accounts_list=["alice"], post_form=http.post_form,
                         fetch_json=http.fetch_json, now_ms=NOW)
    assert again["refreshed"] == again["dead"] == [] and len(http.posts) == 1
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    _assert_no_secret_logged(caplog)


def test_run_once_transient_leaves_bundle_untouched(caplog):
    caplog.set_level(logging.DEBUG)
    b = _bundle()
    _seed({"aws|1": b}, {})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_target"})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert out["transient"] == ["aws-oauth"]
    assert creds.read_json(paths.shared_mcp_path())["aws|1"] == b
    _assert_no_secret_logged(caplog)


def test_run_once_cas_skips_when_refresh_token_changed_concurrently():
    _seed({"aws|1": _bundle()}, {})
    concurrent = _bundle(accessToken="AT-login", refreshToken="RT-login", expiresAt=NOW + 9_999_999)

    def post_form(url, form, headers):
        # A login lands in the store while our refresh is in flight.
        creds.write_json_secure(paths.shared_mcp_path(), {"aws|1": concurrent})
        return 200, json.dumps({"access_token": "AT-NEW-secret", "expires_in": 3600}).encode()

    out = mor.run_once(accounts_list=[], post_form=post_form, fetch_json=FakeHttp().fetch_json, now_ms=NOW)
    assert out["refreshed"] == []
    assert creds.read_json(paths.shared_mcp_path())["aws|1"] == concurrent


def test_run_once_dead_cas_skips_when_refresh_token_changed_concurrently():
    _seed({"aws|1": _bundle()}, {})
    concurrent = _bundle(refreshToken="RT-login", expiresAt=NOW + 9_999_999)

    def post_form(url, form, headers):
        creds.write_json_secure(paths.shared_mcp_path(), {"aws|1": concurrent})
        return 400, json.dumps({"error": "invalid_grant"}).encode()

    out = mor.run_once(accounts_list=[], post_form=post_form, fetch_json=FakeHttp().fetch_json, now_ms=NOW)
    assert out["dead"] == []
    assert creds.read_json(paths.shared_mcp_path())["aws|1"] == concurrent


def test_run_once_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store gone")
    monkeypatch.setattr(mor.creds, "read_shared_mcp", boom)
    monkeypatch.setattr(mor.creds, "sync_cfg_mcp_oauth", boom)
    out = mor.run_once(accounts_list=["alice"], now_ms=NOW)
    assert out == {"refreshed": [], "dead": [], "transient": [], "published": 0,
                   "outcomes": []}


def test_run_once_defaults_to_anthropic_accounts(monkeypatch):
    from viberpit import accounts
    seen = []
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["a1", "a2"])
    monkeypatch.setattr(mor.creds, "sync_cfg_mcp_oauth", lambda cfg, server=None, drop=frozenset(): seen.append(cfg.name) or False)
    mor.run_once(now_ms=NOW)
    assert seen == ["a1", "a2"]


def test_transient_failure_backs_off_before_the_next_attempt():
    _seed({"aws|1": _bundle()}, {})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_client"})] * 3)
    kw = dict(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json)
    assert mor.run_once(now_ms=NOW, **kw)["transient"] == ["aws-oauth"]
    assert mor.run_once(now_ms=NOW + 60_000, **kw)["transient"] == []
    assert len(http.posts) == 1
    assert mor.run_once(now_ms=NOW + int(mor.TRANSIENT_BACKOFF_S * 1000) + 1, **kw)["transient"] == ["aws-oauth"]
    assert len(http.posts) == 2


def _cache_client(client_id):
    creds.write_json_secure(paths.shared_mcp_clients_path(),
                            {mor.url_href(AS_URL): {"client_id": client_id,
                                                    "redirect_uri": "https://e/cb"}})


@pytest.mark.parametrize("code", ["invalid_client", "INVALID_CLIENT"])
def test_invalid_client_forgets_the_stale_cached_registration(code):
    _seed({"aws|1": _bundle()}, {})
    _cache_client("cid")  # the bundle's clientId
    http = FakeHttp(metadata=_meta(), responses=[(401, {"error": code})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json,
                       now_ms=NOW)
    assert out["transient"] == ["aws-oauth"] and out["outcomes"][0]["outcome"] == "transient"
    assert creds.read_mcp_clients() == {}
    assert creds.read_json(paths.shared_mcp_path())["aws|1"] == _bundle()


def test_invalid_client_keeps_a_different_cached_registration():
    _seed({"aws|1": _bundle()}, {})
    _cache_client("cid-newer")
    http = FakeHttp(metadata=_meta(), responses=[(401, {"error": "invalid_client"})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json,
                       now_ms=NOW)
    assert out["transient"] == ["aws-oauth"]
    assert creds.read_mcp_clients()[mor.url_href(AS_URL)]["client_id"] == "cid-newer"


def test_other_transient_codes_keep_the_cached_registration():
    _seed({"aws|1": _bundle()}, {})
    _cache_client("cid")
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_target"})])
    mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert creds.read_mcp_clients()[mor.url_href(AS_URL)]["client_id"] == "cid"


def test_transient_carries_the_oauth_code():
    http = FakeHttp(metadata=_meta(), responses=[(401, {"error": "INVALID_CLIENT"})])
    with pytest.raises(mor.McpRefreshTransient) as exc:
        mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json,
                           now_ms=NOW)
    assert exc.value.code == "INVALID_CLIENT"


# --- resource indicator (RFC 8707) ----------------------------------------------------------

PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"


def test_resource_comes_from_protected_resource_metadata():
    # aws-oauth is configured as ".../mcp?oauth=initialize" but its metadata names ".../mcp";
    # the token endpoint rejects any other resource, which left the grant unrefreshed.
    bundle = _bundle(serverUrl=SERVER_URL + "?oauth=initialize",
                     discoveryState={"authorizationServerUrl": AS_URL, "resourceMetadataUrl": PRM_URL})
    http = FakeHttp(metadata={**_meta(), PRM_URL: {"resource": SERVER_URL}}, responses=[_ok()])
    mor.refresh_bundle(bundle, post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert http.posts[0][1]["resource"] == SERVER_URL


def test_resource_falls_back_to_server_url_without_fragment():
    bundle = _bundle(serverUrl=SERVER_URL + "?x=1#frag",
                     discoveryState={"authorizationServerUrl": AS_URL, "resourceMetadataUrl": PRM_URL})
    http = FakeHttp(metadata=_meta(), responses=[_ok()])  # metadata document unavailable
    mor.refresh_bundle(bundle, post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert http.posts[0][1]["resource"] == SERVER_URL + "?x=1"


@pytest.mark.parametrize("raw, sent", [
    # Railway's metadata names a bare origin. The CLI authorized with `new URL(r).href`, which
    # adds the root path; a refresh naming the raw string drew 400 invalid_target every time.
    ("https://mcp.railway.com", "https://mcp.railway.com/"),
    ("HTTPS://MCP.Example.COM:443", "https://mcp.example.com/"),
    ("https://mcp.example.com:8443/mcp", "https://mcp.example.com:8443/mcp"),
    (SERVER_URL, SERVER_URL),
])
def test_resource_is_sent_as_the_cli_url_href(raw, sent):
    bundle = _bundle(discoveryState={"authorizationServerUrl": AS_URL, "resourceMetadataUrl": PRM_URL})
    http = FakeHttp(metadata={**_meta(), PRM_URL: {"resource": raw}}, responses=[_ok()])
    mor.refresh_bundle(bundle, post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert http.posts[0][1]["resource"] == sent


def test_server_url_fallback_is_sent_as_the_cli_url_href():
    bundle = _bundle(serverUrl="https://mcp.railway.com#frag")
    http = FakeHttp(metadata=_meta(), responses=[_ok()])
    mor.refresh_bundle(bundle, post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert http.posts[0][1]["resource"] == "https://mcp.railway.com/"


def test_transient_failure_is_logged_at_info_with_status_and_code_only(caplog):
    caplog.set_level(logging.INFO)
    _seed({"aws|1": _bundle()}, {})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_target"})])
    mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("aws-oauth" in m and "400" in m and "invalid_target" in m for m in msgs)
    _assert_no_secret_logged(caplog)


def test_dead_grant_codes_match_case_insensitively():
    # AWS sign-in returns upper-case codes; INVALID_TARGET is what it said to the old resource.
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "INVALID_GRANT"}),
                                                 (400, {"error": "INVALID_TARGET"})])
    kw = dict(post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    with pytest.raises(mor.McpGrantDead):
        mor.refresh_bundle(_bundle(), **kw)
    with pytest.raises(mor.McpRefreshTransient):
        mor.refresh_bundle(_bundle(), **kw)


def test_run_once_threads_drop_keys_to_publish(monkeypatch):
    seen = []
    monkeypatch.setattr(mor.creds, "sync_cfg_mcp_oauth",
                        lambda cfg, server=None, drop=frozenset(): seen.append((cfg.name, drop)) or False)
    mor.run_once(accounts_list=["a1", "a2"], drop_keys=frozenset({"old|1"}), now_ms=NOW)
    assert seen == [("a1", frozenset({"old|1"})), ("a2", frozenset({"old|1"}))]


def test_run_once_reports_per_key_outcomes():
    ok, dead, slow = _bundle(serverName="ok"), _bundle(serverName="dead"), _bundle(serverName="slow")
    _seed({"ok|1": ok, "dead|1": dead, "slow|1": slow}, {})
    http = FakeHttp(metadata=_meta(), responses=[_ok(), (400, {"error": "invalid_grant"}),
                                                 (400, {"error": "e" * 900})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    by_key = {o["key"]: o for o in out["outcomes"]}
    assert {k: v["outcome"] for k, v in by_key.items()} == {"ok|1": "refreshed", "dead|1": "dead",
                                                              "slow|1": "transient"}
    assert by_key["ok|1"]["label"] == "ok" and by_key["ok|1"]["error"] is None
    assert by_key["dead|1"]["error"] is None
    assert by_key["slow|1"]["error"] and len(by_key["slow|1"]["error"]) <= 500
    assert out["refreshed"] == ["ok"] and out["dead"] == ["dead"] and out["transient"] == ["slow"]


def test_run_once_skips_superseded_keys():
    _seed({"old|1": _bundle(serverName="old"), "new|1": _bundle(serverName="new")}, {})
    http = FakeHttp(metadata=_meta(), responses=[_ok()])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW,
                       skip_keys=frozenset({"old|1"}))
    assert out["refreshed"] == ["new"] and [o["key"] for o in out["outcomes"]] == ["new|1"]
    assert len(http.posts) == 1
    assert creds.read_json(paths.shared_mcp_path())["old|1"]["refreshToken"] == "RT-OLD-secret"


def test_transient_error_text_has_no_token():
    _seed({"aws|1": _bundle()}, {})
    body = "boom RT-OLD-secret AT-NEW-secret CLIENT-SECRET-x"
    http = FakeHttp(metadata=_meta(), responses=[RuntimeError(body)])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    [o] = out["outcomes"]
    assert o["outcome"] == "transient" and o["error"]
    assert not any(s in o["error"] for s in SECRETS)


@pytest.mark.parametrize("code", ["RT-OLD-secret and more words", "a" * 200, "bad code!"])
def test_oauth_error_code_that_is_not_a_code_is_not_echoed(code):
    _seed({"aws|1": _bundle()}, {})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": code, "error_description": "RT-OLD-secret"})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    [o] = out["outcomes"]
    assert "RT-OLD-secret" not in o["error"] and "400" in o["error"] and len(o["error"]) < 100


@pytest.mark.parametrize("field", ["refreshToken", "clientSecret"])
def test_oauth_error_code_equal_to_a_credential_is_not_echoed(field):
    _seed({"aws|1": _bundle(clientSecret="CLIENT-SECRET-x")}, {})
    secret = {"refreshToken": "RT-OLD-secret", "clientSecret": "CLIENT-SECRET-x"}[field]
    http = FakeHttp(metadata=_meta(token_endpoint_auth_methods_supported=["client_secret_post"]),
                    responses=[(400, {"error": secret})])
    out = mor.run_once(accounts_list=[], post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    [o] = out["outcomes"]
    assert o["error"] == "token endpoint returned 400"


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "http://auth.example.com/token",
                                 "gopher://auth.example.com/token"])
def test_non_https_token_endpoint_is_never_posted_to(bad):
    http = FakeHttp(metadata=_meta(token_endpoint=bad), responses=[_ok()])
    assert mor.token_endpoint(_bundle(), fetch_json=http.fetch_json) == ""
    with pytest.raises(mor.McpRefreshTransient):
        mor.refresh_bundle(_bundle(), post_form=http.post_form, fetch_json=http.fetch_json, now_ms=NOW)
    assert http.posts == []


def test_non_https_auth_server_has_no_fallback_endpoint():
    b = _bundle(discoveryState={"authorizationServerUrl": "http://auth.example.com/"})
    assert mor.token_endpoint(b, fetch_json=FakeHttp().fetch_json) == ""


# --- lead scales with a short token lifetime ------------------------------------------------

def test_short_lived_key_is_due_only_in_its_last_half():
    lifetimes = {"k": 300.0}
    four_min = {"k": _bundle(expiresAt=NOW + 240_000)}
    two_min = {"k": _bundle(expiresAt=NOW + 120_000)}
    assert mor.due_keys(four_min, NOW, lifetimes=lifetimes) == []
    assert mor.due_keys(two_min, NOW, lifetimes=lifetimes) == ["k"]


def test_unknown_lifetime_keeps_the_full_lead():
    store = {"k": _bundle(expiresAt=NOW + 240_000)}
    assert mor.due_keys(store, NOW) == ["k"]
    assert mor.due_keys(store, NOW, lifetimes={"other": 300.0}) == ["k"]


def test_long_lifetime_never_exceeds_the_lead():
    store = {"k": _bundle(expiresAt=NOW + 700_000)}
    assert mor.due_keys(store, NOW, lifetimes={"k": 3600.0}) == []


def test_refresh_records_lifetime_and_is_not_repeated_next_tick():
    _seed({"aws|1": _bundle(expiresAt=NOW + 60_000)}, {"alice": {}})
    http = FakeHttp(metadata=_meta(), responses=[_ok(expires_in=300), _ok(expires_in=300)])
    kw = dict(accounts_list=["alice"], post_form=http.post_form, fetch_json=http.fetch_json)
    assert mor.run_once(now_ms=NOW, **kw)["refreshed"] == ["aws-oauth"]
    assert mor._lifetime_s == {"aws|1": 300.0}
    again = mor.run_once(now_ms=NOW + 60_000, **kw)
    assert again["refreshed"] == [] and len(http.posts) == 1
    assert mor.run_once(now_ms=NOW + 160_000, **kw)["refreshed"] == ["aws-oauth"]


def test_dead_clear_drops_the_lifetime():
    mor._lifetime_s["aws|1"] = 300.0
    _seed({"aws|1": _bundle(expiresAt=NOW - 5)}, {"alice": {}})
    http = FakeHttp(metadata=_meta(), responses=[(400, {"error": "invalid_grant"})])
    out = mor.run_once(accounts_list=["alice"], post_form=http.post_form,
                       fetch_json=http.fetch_json, now_ms=NOW)
    assert out["dead"] == ["aws-oauth"] and "aws|1" not in mor._lifetime_s


@pytest.mark.parametrize("expires_in", [0, -5, float("inf"), float("nan"), True, "300", None])
def test_unusable_expires_in_records_no_lifetime(expires_in):
    _seed({"aws|1": _bundle(expiresAt=NOW + 60_000)}, {"alice": {}})
    http = FakeHttp(metadata=_meta(), responses=[_ok(expires_in=expires_in)])
    mor.run_once(accounts_list=["alice"], post_form=http.post_form,
                 fetch_json=http.fetch_json, now_ms=NOW)
    assert mor._lifetime_s == {}
