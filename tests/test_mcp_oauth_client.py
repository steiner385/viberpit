"""ViberPit's own MCP OAuth client (phase 4 task 2): fake HTTP, isolated store."""
import base64
import hashlib
import json
import os
import stat
import urllib.parse

import pytest

from viberpit import mcp_oauth_client as moc, paths

NOW = 1_000_000_000_000
REDIRECT = "https://dobby.example.ts.net/api/mcp/oauth/callback"
SECRETS = ("CODE-secret-abc", "VERIFIER-secret-xyz", "AT-secret-123", "RT-secret-456",
           "CLIENT-SECRET-789")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))


# --- provider fakes (shapes from the 2026-11-04 probe table) --------------------------------

AWS_SERVER = "https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize"
AWS_AS = "https://us-east-1.oauth.signin.aws/"
RAILWAY_SERVER = "https://mcp.railway.com"
RAILWAY_AS = "https://backboard.railway.com"
KINDASH_SERVER = "https://qa.kindash.com/mcp"
KINDASH_AS = "https://qa.kindash.com"


def _as_meta(issuer: str, *, register: str | None, scopes=None) -> dict:
    doc = {"issuer": issuer, "authorization_endpoint": f"{issuer.rstrip('/')}/authorize",
           "token_endpoint": f"{issuer.rstrip('/')}/token",
           "code_challenge_methods_supported": ["S256"]}
    if register:
        doc["registration_endpoint"] = register
    if scopes is not None:
        doc["scopes_supported"] = scopes
    return doc


PROVIDERS = {
    "aws": {
        "server": AWS_SERVER,
        "docs": {
            "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp":
                {"resource": "https://aws-mcp.us-east-1.api.aws/mcp", "authorization_servers": [AWS_AS]},
            "https://us-east-1.oauth.signin.aws/.well-known/oauth-authorization-server":
                _as_meta(AWS_AS, register="https://us-east-1.oauth.signin.aws/v1/register"),
        },
        "headers": {},
    },
    "railway": {
        "server": RAILWAY_SERVER,
        "docs": {
            "https://mcp.railway.com/.well-known/oauth-protected-resource":
                {"resource": "https://mcp.railway.com", "authorization_servers": [RAILWAY_AS],
                 "scopes_supported": ["offline_access", "workspace:member"]},
            "https://backboard.railway.com/.well-known/oauth-authorization-server":
                _as_meta(RAILWAY_AS, register="https://backboard.railway.com/oauth/register",
                         scopes=["openid", "offline_access", "workspace:member"]),
        },
        "headers": {},
    },
    "kindash": {
        "server": KINDASH_SERVER,
        "docs": {
            # The path-insert URL 404s; the root PRM is reachable only via WWW-Authenticate.
            "https://qa.kindash.com/.well-known/oauth-protected-resource":
                {"resource": "https://qa.kindash.com/mcp", "authorization_servers": [KINDASH_AS]},
            "https://qa.kindash.com/.well-known/openid-configuration":
                _as_meta(KINDASH_AS, register=None, scopes=["openid", "offline_access"]),
        },
        "headers": {"WWW-Authenticate": 'Bearer realm="kindash", '
                    'resource_metadata="https://qa.kindash.com/.well-known/oauth-protected-resource"'},
    },
}


class Fake:
    def __init__(self, docs: dict, headers: dict | None = None):
        self.docs = docs
        self.headers = headers or {}
        self.fetched: list[str] = []
        self.probed: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        return self.docs.get(url)

    def probe(self, url):
        self.probed.append(url)
        return self.headers


def _discover(name: str):
    p = PROVIDERS[name]
    fake = Fake(p["docs"], p["headers"])
    return moc.discover(p["server"], fetch_json=fake.fetch, probe=fake.probe), fake


class Poster:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, url, body, *rest):
        self.calls.append((url, body, *rest))
        status, payload = self.responses.pop(0)
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


# --- discovery ----------------------------------------------------------------------------

def test_discover_aws_path_insert():
    d, fake = _discover("aws")
    assert d.server_url == AWS_SERVER
    assert d.prm_url == "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp"
    assert d.resource == "https://aws-mcp.us-east-1.api.aws/mcp"
    assert d.authorization_server == AWS_AS
    assert d.as_metadata["registration_endpoint"].endswith("/v1/register")
    assert d.as_metadata_url == "https://us-east-1.oauth.signin.aws/.well-known/oauth-authorization-server"
    assert d.scopes_supported == ()
    assert fake.probed == [AWS_SERVER]


def test_discover_railway_root_and_href_resource():
    d, _ = _discover("railway")
    assert d.prm_url == "https://mcp.railway.com/.well-known/oauth-protected-resource"
    assert d.resource == "https://mcp.railway.com/"  # url_href, as the refresher sends it
    assert d.authorization_server == "https://backboard.railway.com/"
    assert d.scopes_supported == ("offline_access", "workspace:member")


def test_discover_kindash_via_www_authenticate_first():
    d, fake = _discover("kindash")
    assert d.prm_url == "https://qa.kindash.com/.well-known/oauth-protected-resource"
    assert fake.fetched[0] == d.prm_url  # the header's URL is tried before path-insert
    assert d.as_metadata_url == "https://qa.kindash.com/.well-known/openid-configuration"


def test_discover_header_key_is_case_insensitive():
    p = PROVIDERS["kindash"]
    fake = Fake(p["docs"], {"www-authenticate": p["headers"]["WWW-Authenticate"]})
    d = moc.discover(KINDASH_SERVER, fetch_json=fake.fetch, probe=fake.probe)
    assert fake.fetched[0] == d.prm_url


def test_discover_falls_back_to_root_after_path_insert_miss():
    p = PROVIDERS["kindash"]
    fake = Fake(p["docs"], {})
    d = moc.discover(KINDASH_SERVER, fetch_json=fake.fetch, probe=fake.probe)
    assert fake.fetched[:2] == ["https://qa.kindash.com/.well-known/oauth-protected-resource/mcp",
                                "https://qa.kindash.com/.well-known/oauth-protected-resource"]
    assert d.resource == "https://qa.kindash.com/mcp"


def test_discover_survives_a_failing_probe():
    p = PROVIDERS["aws"]
    fake = Fake(p["docs"])

    def boom(url):
        raise OSError("down")

    d = moc.discover(AWS_SERVER, fetch_json=fake.fetch, probe=boom)
    assert d.resource == "https://aws-mcp.us-east-1.api.aws/mcp"


def test_discover_rejects_resource_on_another_origin():
    docs = {"https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            {"resource": "https://evil.example.net/mcp", "authorization_servers": [AWS_AS]},
            **PROVIDERS["aws"]["docs"]}
    fake = Fake(docs)
    with pytest.raises(moc.DiscoveryFailed):
        moc.discover("https://mcp.example.com/mcp", fetch_json=fake.fetch, probe=fake.probe)


def test_discover_rejects_header_pointing_at_another_origin():
    # The PRM is self-consistent but lives on, and names, another origin than the server.
    evil_prm = "https://evil.example.net/.well-known/oauth-protected-resource"
    docs = {evil_prm: {"resource": "https://evil.example.net/", "authorization_servers": [AWS_AS]},
            **PROVIDERS["aws"]["docs"]}
    fake = Fake(docs, {"WWW-Authenticate": f'Bearer resource_metadata="{evil_prm}"'})
    with pytest.raises(moc.DiscoveryFailed):
        moc.discover("https://mcp.example.com/mcp", fetch_json=fake.fetch, probe=fake.probe)


def test_discover_without_prm_fails():
    fake = Fake({})
    with pytest.raises(moc.DiscoveryFailed):
        moc.discover("https://mcp.example.com/mcp", fetch_json=fake.fetch, probe=fake.probe)


def test_discover_without_as_metadata_fails():
    docs = {k: v for k, v in PROVIDERS["aws"]["docs"].items() if "oauth-protected-resource" in k}
    fake = Fake(docs)
    with pytest.raises(moc.DiscoveryFailed):
        moc.discover(AWS_SERVER, fetch_json=fake.fetch, probe=fake.probe)


def test_discover_non_http_server_fails():
    with pytest.raises(moc.DiscoveryFailed):
        moc.discover("stdio://x", fetch_json=Fake({}).fetch, probe=Fake({}).probe)


# --- scope / pkce / state ---------------------------------------------------------------------

def test_choose_scope():
    assert moc.choose_scope(_discover("railway")[0]) == "offline_access workspace:member"
    assert moc.choose_scope(_discover("kindash")[0]) == "offline_access"
    assert moc.choose_scope(_discover("aws")[0]) == ""


def test_choose_scope_adds_offline_access_the_as_supports():
    docs = dict(PROVIDERS["railway"]["docs"])
    prm = "https://mcp.railway.com/.well-known/oauth-protected-resource"
    docs[prm] = {**docs[prm], "scopes_supported": ["workspace:member"]}
    fake = Fake(docs)
    d = moc.discover(RAILWAY_SERVER, fetch_json=fake.fetch, probe=fake.probe)
    assert moc.choose_scope(d) == "workspace:member offline_access"


def test_pkce_pair():
    verifier, challenge = moc.pkce_pair()
    assert len(verifier) == 64
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in challenge
    assert moc.pkce_pair()[0] != verifier


def test_new_state():
    s = moc.new_state()
    assert len(s) >= 43 and s != moc.new_state()


# --- registration -------------------------------------------------------------------------

def test_ensure_client_registers_with_the_exact_body_and_caches():
    d, _ = _discover("aws")
    post = Poster((201, {"client_id": "cid-1"}))
    client = moc.ensure_client(d, REDIRECT, post_json=post)
    assert client == moc.Client(client_id="cid-1", client_secret=None, redirect_uri=REDIRECT)
    url, body = post.calls[0]
    assert url == "https://us-east-1.oauth.signin.aws/v1/register"
    assert body == {"client_name": "ViberPit", "redirect_uris": [REDIRECT],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none"}
    path = paths.shared_mcp_clients_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    cache = json.loads(path.read_text())
    entry = cache[AWS_AS]
    assert entry["client_id"] == "cid-1" and entry["redirect_uri"] == REDIRECT
    assert "client_secret" not in entry and entry["registered_at"]


def test_ensure_client_reuses_the_cache():
    d, _ = _discover("aws")
    moc.ensure_client(d, REDIRECT, post_json=Poster((201, {"client_id": "cid-1"})))
    post = Poster()
    assert moc.ensure_client(d, REDIRECT, post_json=post).client_id == "cid-1"
    assert post.calls == []


def test_ensure_client_re_registers_when_redirect_changes():
    d, _ = _discover("aws")
    moc.ensure_client(d, REDIRECT, post_json=Poster((201, {"client_id": "cid-1"})))
    other = "https://other.example.ts.net/api/mcp/oauth/callback"
    post = Poster((201, {"client_id": "cid-2", "client_secret": "CLIENT-SECRET-789"}))
    client = moc.ensure_client(d, other, post_json=post)
    assert client == moc.Client("cid-2", "CLIENT-SECRET-789", other)
    assert json.loads(paths.shared_mcp_clients_path().read_text())[AWS_AS]["client_id"] == "cid-2"


def test_ensure_client_honours_cache_path(tmp_path):
    d, _ = _discover("aws")
    cache = tmp_path / "c.json"
    moc.ensure_client(d, REDIRECT, post_json=Poster((200, {"client_id": "cid-1"})), cache_path=cache)
    assert json.loads(cache.read_text())[AWS_AS]["client_id"] == "cid-1"
    assert not paths.shared_mcp_clients_path().exists()


def test_forget_client_forces_re_registration():
    d, _ = _discover("railway")
    moc.ensure_client(d, REDIRECT, post_json=Poster((201, {"client_id": "cid-1"})))
    moc.forget_client("https://backboard.railway.com")  # any href spelling of the issuer
    post = Poster((201, {"client_id": "cid-2"}))
    assert moc.ensure_client(d, REDIRECT, post_json=post).client_id == "cid-2"


def test_ensure_client_kindash_unsupported():
    d, _ = _discover("kindash")
    with pytest.raises(moc.RegistrationUnsupported):
        moc.ensure_client(d, REDIRECT, post_json=Poster())


@pytest.mark.parametrize("status,payload", [
    (400, {"error": "invalid_redirect_uri", "error_description": "CLIENT-SECRET-789 no"}),
    (201, {"client_secret": "CLIENT-SECRET-789"}),
    (500, b"<html>CLIENT-SECRET-789</html>"),
])
def test_ensure_client_failure_is_sanitised(status, payload):
    d, _ = _discover("aws")
    with pytest.raises(moc.RegistrationFailed) as exc:
        moc.ensure_client(d, REDIRECT, post_json=Poster((status, payload)))
    assert str(status) in str(exc.value)
    assert "CLIENT-SECRET-789" not in str(exc.value)
    assert not paths.shared_mcp_clients_path().exists()


def test_registration_error_code_survives():
    d, _ = _discover("aws")
    with pytest.raises(moc.RegistrationFailed, match="400 invalid_redirect_uri"):
        moc.ensure_client(d, REDIRECT, post_json=Poster((400, {"error": "invalid_redirect_uri"})))


# --- authorize URL ------------------------------------------------------------------------

def test_authorize_url():
    d, _ = _discover("railway")
    client = moc.Client("cid-1", None, REDIRECT)
    url = moc.authorize_url(d, client, state="ST", code_challenge="CH",
                            scope="offline_access workspace:member")
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://backboard.railway.com/authorize"
    assert dict(urllib.parse.parse_qsl(parts.query)) == {
        "response_type": "code", "client_id": "cid-1", "redirect_uri": REDIRECT,
        "code_challenge": "CH", "code_challenge_method": "S256", "state": "ST",
        "resource": "https://mcp.railway.com/", "scope": "offline_access workspace:member"}


def test_authorize_url_omits_empty_scope_and_keeps_endpoint_query():
    d, _ = _discover("aws")
    d = moc.Discovery(**{**d.__dict__, "as_metadata": {
        **d.as_metadata, "authorization_endpoint": "https://us-east-1.oauth.signin.aws/authorize?tenant=x"}})
    url = moc.authorize_url(d, moc.Client("cid", None, REDIRECT), state="S", code_challenge="C", scope="")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert "scope" not in query and query["tenant"] == "x"


# --- code exchange ------------------------------------------------------------------------

def test_exchange_code_builds_the_cli_bundle():
    d, _ = _discover("railway")
    client = moc.Client("cid-1", None, REDIRECT)
    post = Poster((200, {"access_token": "AT-secret-123", "refresh_token": "RT-secret-456",
                         "expires_in": 3600, "scope": "offline_access workspace:member",
                         "token_type": "Bearer"}))
    bundle = moc.exchange_code(d, client, code="CODE-secret-abc", verifier="VERIFIER-secret-xyz",
                               server_name="plugin:railway:railway", now_ms=NOW, post_form=post)
    url, form, headers = post.calls[0]
    assert url == "https://backboard.railway.com/token"
    assert form == {"grant_type": "authorization_code", "code": "CODE-secret-abc",
                    "redirect_uri": REDIRECT, "client_id": "cid-1",
                    "code_verifier": "VERIFIER-secret-xyz", "resource": "https://mcp.railway.com/"}
    assert headers == {}
    assert bundle == {
        "serverName": "plugin:railway:railway", "serverUrl": RAILWAY_SERVER,
        "accessToken": "AT-secret-123", "refreshToken": "RT-secret-456",
        "expiresAt": NOW + 3_600_000, "scope": "offline_access workspace:member",
        "clientId": "cid-1", "redirectUri": REDIRECT,
        "discoveryState": {
            "authorizationServerUrl": "https://backboard.railway.com/",
            "resourceMetadataUrl": "https://mcp.railway.com/.well-known/oauth-protected-resource",
            "authServerMetadataUrl":
                "https://backboard.railway.com/.well-known/oauth-authorization-server",
            "oauthMetadataFound": True}}


def test_exchange_code_access_only_response():
    d, _ = _discover("aws")
    post = Poster((200, {"access_token": "AT-secret-123"}))
    bundle = moc.exchange_code(d, moc.Client("cid", None, REDIRECT), code="C", verifier="V",
                               server_name="aws-oauth", now_ms=NOW, post_form=post)
    assert bundle["accessToken"] == "AT-secret-123"
    assert "refreshToken" not in bundle and "expiresAt" not in bundle and "scope" not in bundle
    assert "clientSecret" not in bundle


def test_exchange_code_sends_client_secret_post():
    d, _ = _discover("aws")
    post = Poster((200, {"access_token": "AT-secret-123", "expires_in": 60}))
    bundle = moc.exchange_code(d, moc.Client("cid", "CLIENT-SECRET-789", REDIRECT), code="C",
                               verifier="V", server_name="aws-oauth", now_ms=NOW, post_form=post)
    assert post.calls[0][1]["client_secret"] == "CLIENT-SECRET-789"
    assert bundle["clientSecret"] == "CLIENT-SECRET-789"


@pytest.mark.parametrize("status,payload", [
    (400, {"error": "invalid_grant", "error_description": "code CODE-secret-abc VERIFIER-secret-xyz"}),
    (400, {"error": "CODE-secret-abc"}),  # an endpoint echoing the code as the "error"
    (200, {"refresh_token": "RT-secret-456"}),  # no access token
    (502, b"<html>AT-secret-123 CODE-secret-abc</html>"),
])
def test_exchange_code_failure_is_sanitised(status, payload):
    d, _ = _discover("aws")
    with pytest.raises(moc.ExchangeFailed) as exc:
        moc.exchange_code(d, moc.Client("cid", "CLIENT-SECRET-789", REDIRECT),
                          code="CODE-secret-abc", verifier="VERIFIER-secret-xyz",
                          server_name="aws-oauth", now_ms=NOW, post_form=Poster((status, payload)))
    text = str(exc.value)
    assert str(status) in text
    assert not any(s in text for s in SECRETS)


def test_exchange_code_transport_error_is_sanitised():
    d, _ = _discover("aws")

    def boom(url, form, headers):
        raise OSError("CODE-secret-abc")

    with pytest.raises(moc.ExchangeFailed) as exc:
        moc.exchange_code(d, moc.Client("cid", None, REDIRECT), code="CODE-secret-abc",
                          verifier="VERIFIER-secret-xyz", server_name="aws-oauth", now_ms=NOW,
                          post_form=boom)
    assert "CODE-secret-abc" not in str(exc.value)
