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
