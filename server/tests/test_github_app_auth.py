import base64
import json
import logging

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

from server.github.app_auth import mint_jwt, AppJwtSigner
from server.github.app_auth import (
    InstallationRegistry, AppTokenSource, GhAppError, REFRESH_MARGIN_S,
)


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def _b64url_json(seg: str) -> dict:
    pad = "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg + pad))


def test_mint_jwt_claims_and_signature_verify():
    key, pem = _keypair()
    now = 1_700_000_000.0
    tok = mint_jwt(4031757, pem, now)
    head_b64, payload_b64, sig_b64 = tok.split(".")
    assert _b64url_json(head_b64) == {"alg": "RS256", "typ": "JWT"}
    payload = _b64url_json(payload_b64)
    nows = int(now)
    assert payload == {"iat": nows - 60, "exp": nows + 540, "iss": "4031757"}
    # signature verifies over "head.payload" with the public key
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    key.public_key().verify(
        sig, f"{head_b64}.{payload_b64}".encode(),
        padding.PKCS1v15(), hashes.SHA256(),
    )  # raises on mismatch


def test_signer_loads_pem_once_and_mints():
    _key, pem = _keypair()
    signer = AppJwtSigner(4031757, pem, clock=lambda: 1_700_000_000.0)
    assert signer.app_id == 4031757
    assert signer.mint().count(".") == 2


def test_signer_rejects_bad_pem():
    with pytest.raises(Exception):
        AppJwtSigner(1, "not a pem", clock=lambda: 0.0)


# ---------------------------------------------------------------------------
# Task 2: InstallationRegistry + AppTokenSource + GhAppError + REFRESH_MARGIN_S
# ---------------------------------------------------------------------------

class _FakeRest:
    """Records calls; returns canned JSON per (method, path)."""
    def __init__(self, routes): self.routes = routes; self.calls = []
    def __call__(self, method, path, jwt):
        self.calls.append((method, path))
        for (m, p), resp in self.routes.items():
            if m == method and (p == path or (p.endswith("*") and path.startswith(p[:-1]))):
                if isinstance(resp, Exception): raise resp
                return resp
        raise GhAppError(f"GitHub App REST {method} {path}: HTTP 404")


def _signer():
    _key, pem = _keypair()
    return AppJwtSigner(4031757, pem, clock=lambda: 1_700_000_000.0)


def test_registry_maps_owner_case_insensitive():
    rest = _FakeRest({("GET", "/app/installations"): [
        {"id": 11, "account": {"login": "cairnea"}},
        {"id": 22, "account": {"login": "Steiner385"}},
    ]})
    reg = InstallationRegistry(_signer(), rest)
    reg.load()
    assert reg.installation_for("cairnea") == 11
    assert reg.installation_for("steiner385") == 22   # case-insensitive
    assert reg.installation_for("unknown") is None


def test_registry_no_installations_errors():
    rest = _FakeRest({("GET", "/app/installations"): []})
    with pytest.raises(GhAppError):
        InstallationRegistry(_signer(), rest).load()


def test_token_source_caches_until_margin_then_refreshes():
    t = [1_700_000_000.0]
    rest = _FakeRest({
        ("POST", "/app/installations/11/access_tokens"):
            # expires_at = clock + 3600 so (expires_at - now)=3600 > REFRESH_MARGIN_S=300
            {"token": "ghs_AAA", "expires_at": "2023-11-14T23:13:20Z"},
    })
    # expires_at parses to an epoch; first get mints
    src = AppTokenSource(_signer(), 11, rest, clock=lambda: t[0])
    tok1 = src.get()
    assert tok1 == "ghs_AAA"
    assert len(rest.calls) == 1   # minted once
    tok2 = src.get()
    assert tok2 == "ghs_AAA" and len(rest.calls) == 1  # cached, no second mint
    # Advance time to within the margin: expires_at (1700003600) - new_now = REFRESH_MARGIN_S
    # new_now = expires_at - REFRESH_MARGIN_S = 1700003600 - 300 = 1700003300
    t[0] = 1_700_003_600.0 - REFRESH_MARGIN_S
    tok3 = src.get()
    assert tok3 == "ghs_AAA" and len(rest.calls) == 2  # re-minted due to margin breach


def test_token_source_refresh_forces_remint():
    t = [1_700_000_000.0]
    rest = _FakeRest({
        ("POST", "/app/installations/11/access_tokens"):
            {"token": "ghs_AAA", "expires_at": "2023-11-14T23:13:20Z"},
    })
    src = AppTokenSource(_signer(), 11, rest, clock=lambda: t[0])
    tok1 = src.get()
    assert tok1 == "ghs_AAA"
    assert len(rest.calls) == 1  # minted once
    # refresh() must force re-mint unconditionally
    tok2 = src.refresh()
    assert tok2 == "ghs_AAA" and len(rest.calls) == 2  # re-minted


# ---------------------------------------------------------------------------
# Task 3: token_for_owner facade (singleton, fallback, warn-once)
# ---------------------------------------------------------------------------
from server.github import app_auth as A  # noqa: E402


def test_token_for_owner_none_when_unconfigured(monkeypatch):
    A.reset()
    monkeypatch.setattr(A, "_load_app_config", lambda: None)  # nothing configured
    assert A.token_for_owner("cairnea") is None


def test_token_for_owner_routes_per_owner(monkeypatch, tmp_path, caplog):
    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({
        ("GET", "/app/installations"): [
            {"id": 11, "account": {"login": "cairnea"}},
            {"id": 22, "account": {"login": "steiner385"}},
        ],
        ("POST", "/app/installations/11/access_tokens"):
            {"token": "ghs_CA", "expires_at": "2099-01-01T00:00:00Z"},
        ("POST", "/app/installations/22/access_tokens"):
            {"token": "ghs_ST", "expires_at": "2099-01-01T00:00:00Z"},
    })
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    assert A.token_for_owner("cairnea") == "ghs_CA"
    assert A.token_for_owner("steiner385") == "ghs_ST"
    # Uncovered owner warns once, not twice
    with caplog.at_level(logging.WARNING):
        assert A.token_for_owner("nobody") is None   # first call — warns
        assert A.token_for_owner("nobody") is None   # second call — must NOT warn again
    assert caplog.text.count("owner_no_installation") == 1


def test_token_for_owner_swallows_mint_error(monkeypatch, caplog):
    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({("GET", "/app/installations"): GhAppError("boom")})
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    assert A.token_for_owner("cairnea") is None
    assert "ghs_" not in caplog.text  # no token leaked


# ---------------------------------------------------------------------------
# installation_owners facade — repo enumeration needs the owner list up front
# ---------------------------------------------------------------------------


def test_installation_owners_lists_every_installation(monkeypatch):
    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({
        ("GET", "/app/installations"): [
            {"id": 11, "account": {"login": "cairnea"}},
            {"id": 22, "account": {"login": "steiner385"}},
        ],
    })
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    assert sorted(A.installation_owners()) == ["cairnea", "steiner385"]


def test_installation_owners_empty_when_unconfigured(monkeypatch):
    # No App configured: the caller must see "no installations", never a crash.
    A.reset()
    monkeypatch.setattr(A, "_load_app_config", lambda: None)
    assert A.installation_owners() == []


def test_register_gitrepos_providers_wires_both_seams(monkeypatch):
    """Both seams must be registered together.

    Registering only the token provider is what left the edge process listing
    repos with the machine's ambient gh credential: the token seam alone cannot
    enumerate owners, so enumeration silently stayed on the user path.
    """
    from ccswitch import gitrepos

    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({
        ("GET", "/app/installations"): [
            {"id": 11, "account": {"login": "cairnea"}},
        ],
    })
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    gitrepos.set_token_provider(None)
    gitrepos.set_installations_provider(None)
    try:
        A.register_gitrepos_providers()
        assert gitrepos._token_provider is not None
        # Routed through the App facade, not a second enumeration of its own.
        assert gitrepos._installation_owners() == ["cairnea"]
    finally:
        gitrepos.set_token_provider(None)
        gitrepos.set_installations_provider(None)


def test_registration_without_a_configured_app_keeps_the_viewer_path(monkeypatch):
    """Registering the seams must not, by itself, commit to the App path.

    Both processes register unconditionally at startup, before anyone knows
    whether an App is configured. If "registered" implied "App path", a
    deployment with no GitHub App would hard-fail repo listing instead of
    falling back to the operator's own gh login.
    """
    from ccswitch import gitrepos

    A.reset()
    monkeypatch.setattr(A, "_load_app_config", lambda: None)  # nothing configured
    gitrepos.set_token_provider(None)
    gitrepos.set_installations_provider(None)
    try:
        A.register_gitrepos_providers()
        # None -> "no App here, use the viewer path"; [] would mean "App is
        # configured but has zero installations", which IS a loud error.
        assert gitrepos._installation_owners() is None
    finally:
        gitrepos.set_token_provider(None)
        gitrepos.set_installations_provider(None)


def test_installation_owners_empty_when_registry_fails(monkeypatch, caplog):
    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({("GET", "/app/installations"): GhAppError("boom")})
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    assert A.installation_owners() == []
    assert "ghs_" not in caplog.text  # no token leaked
