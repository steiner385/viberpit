import base64
import json

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
