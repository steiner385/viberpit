"""GitHub App installation-token auth for the PR poller — a Python port of
Plotroom's server/auth.ts (AppJwtSigner + InstallationRegistry + AppTokenSource).
Sync (runs inside the poll's asyncio.to_thread); never logs the token/JWT/key."""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Callable, cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

log = logging.getLogger("server.github.app_auth")

JWT_BACKDATE_S = 60
JWT_TTL_S = 540


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_jwt(app_id: int, private_key_pem: str, now: float) -> str:
    """RS256 App JWT over b64url(header).b64url(payload), signed with the PEM key."""
    key = cast(RSAPrivateKey, serialization.load_pem_private_key(private_key_pem.encode(), password=None))
    nows = int(now)
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(
        {"iat": nows - JWT_BACKDATE_S, "exp": nows + JWT_TTL_S, "iss": str(app_id)}
    ).encode())
    signing_input = f"{head}.{payload}".encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64url(sig)}"


class AppJwtSigner:
    """Loads/validates the PEM ONCE; mints RS256 App JWTs."""

    def __init__(self, app_id: int, private_key_pem: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.app_id = app_id
        self._pem = private_key_pem
        # validate eagerly (raises on a bad PEM); cast asserts RSA key type
        cast(RSAPrivateKey, serialization.load_pem_private_key(private_key_pem.encode(), password=None))
        self._clock = clock

    def mint(self) -> str:
        return mint_jwt(self.app_id, self._pem, self._clock())
