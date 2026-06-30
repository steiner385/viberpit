import base64, hashlib
from urllib.parse import urlparse, parse_qs
from ccswitch import setup_token_oauth as sto


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def test_begin_pkce_challenge_is_s256_of_verifier():
    c = sto.begin()
    q = parse_qs(urlparse(c.authorize_url).query)
    assert q["code_challenge_method"] == ["S256"]
    expected = _b64url_nopad(hashlib.sha256(c.verifier.encode("ascii")).digest())
    assert q["code_challenge"] == [expected]


def test_begin_url_has_required_params():
    c = sto.begin()
    p = urlparse(c.authorize_url)
    assert p.scheme == "https" and p.netloc == "claude.com" and p.path == "/cai/oauth/authorize"
    q = parse_qs(p.query)
    assert q["client_id"] == [sto.CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == [sto.REDIRECT_URI]
    assert q["scope"] == [sto.SCOPE]
    assert q["state"] == [c.state]
    assert q["code"] == ["true"]


def test_begin_verifier_not_leaked_in_url_and_state_is_random():
    c1 = sto.begin()
    c2 = sto.begin()
    assert c1.verifier not in c1.authorize_url      # verifier stays server-side
    assert c1.state != c2.state                      # random per call
    assert c1.verifier != c2.verifier


import httpx
import pytest


def _challenge():
    c = sto.begin()
    return c


def _exchange_with_handler(monkeypatch, handler, pasted):
    """Patch httpx.post (sync) with a fake that returns handler(url, **kwargs)."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return handler(url, **kwargs)

    monkeypatch.setattr(sto.httpx, "post", fake_post)
    c = sto.begin()
    # build a pasted code whose #state matches unless overridden
    code = pasted if pasted is not None else f"AUTHCODE#{c.state}"
    return c, code, captured


def test_exchange_success_returns_token(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(200, json={"access_token": "sk-ant-oat01-REALTOKEN0123"})
    c, code, captured = _exchange_with_handler(monkeypatch, handler, None)
    tok = sto.exchange(code, c)
    assert tok == "sk-ant-oat01-REALTOKEN0123"
    body = captured["kwargs"].get("json") or captured["kwargs"].get("data")
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == c.verifier
    assert body["client_id"] == sto.CLIENT_ID
    assert body["redirect_uri"] == sto.REDIRECT_URI
    assert body["expires_in"] == 31536000   # required for the 1-yr token (verified)
    headers = captured["kwargs"].get("headers", {})
    assert "User-Agent" in headers


def test_exchange_state_mismatch_raises_before_http(monkeypatch):
    called = {"n": 0}
    def handler(url, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"access_token": "sk-ant-oat01-X"})
    c, _, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenStateMismatch):
        sto.exchange("AUTHCODE#WRONGSTATE", c)
    assert called["n"] == 0  # never hit the network


def test_exchange_invalid_code_raises(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(400, json={"error": "invalid_grant"})
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenInvalidCode):
        sto.exchange(code, c)


def test_exchange_network_error_is_transient(monkeypatch):
    def handler(url, **kw):
        raise httpx.ConnectError("boom")
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenTransient):
        sto.exchange(code, c)


def test_exchange_200_without_token_raises(monkeypatch):
    def handler(url, **kw):
        return httpx.Response(200, json={"nope": 1})
    c, code, _ = _exchange_with_handler(monkeypatch, handler, None)
    with pytest.raises(sto.SetupTokenNoToken):
        sto.exchange(code, c)
