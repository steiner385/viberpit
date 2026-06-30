import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

from server.github.app_auth import mint_jwt, AppJwtSigner


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
    import pytest
    with pytest.raises(Exception):
        AppJwtSigner(1, "not a pem", clock=lambda: 0.0)
