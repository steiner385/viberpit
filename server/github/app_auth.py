"""GitHub App installation-token auth for the PR poller — a Python port of
Plotroom's server/auth.ts (AppJwtSigner + InstallationRegistry + AppTokenSource).
Sync (runs inside the poll's asyncio.to_thread); never logs the token/JWT/key."""
from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import time
from typing import Any, Callable, cast

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


# ---------------------------------------------------------------------------
# Task 2: GhAppError, AppRest, default_app_rest, InstallationRegistry,
#          AppTokenSource, REFRESH_MARGIN_S
# ---------------------------------------------------------------------------

import httpx  # noqa: E402  (stdlib imports above; httpx is third-party)

REFRESH_MARGIN_S = 300  # 5 * 60_000 ms, mirrors Plotroom's TOKEN_REFRESH_MARGIN_MS
_REST_BASE = "https://api.github.com"


class GhAppError(Exception):
    pass


AppRest = Callable[[str, str, str], Any]


def default_app_rest(method: str, path: str, jwt: str) -> Any:
    with httpx.Client(timeout=15.0) as client:
        res = client.request(
            method, f"{_REST_BASE}{path}",
            headers={
                "authorization": f"Bearer {jwt}",
                "accept": "application/vnd.github+json",
                "user-agent": "ccswitch",
            },
        )
    if res.status_code // 100 != 2:
        raise GhAppError(f"GitHub App REST {method} {path}: HTTP {res.status_code}")
    return res.json()


def _parse_expires(iso: str) -> float:
    return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class InstallationRegistry:
    """owner login (lowercased) → installation id."""

    def __init__(self, signer: AppJwtSigner, app_rest: AppRest,
                 installation_id: int | None = None) -> None:
        self._signer = signer
        self._rest = app_rest
        self._restrict_to = installation_id
        self._by_login: dict[str, int] = {}

    def load(self) -> None:
        jwt = self._signer.mint()
        if self._restrict_to is not None:
            inst = self._rest("GET", f"/app/installations/{self._restrict_to}", jwt)
            login = ((inst or {}).get("account") or {}).get("login")
            if not login:
                raise GhAppError(
                    f'tokenSource "app": installation {self._restrict_to} has no account login')
            self._by_login = {login.lower(): self._restrict_to}
            return
        raw = self._rest("GET", "/app/installations", jwt)
        if not isinstance(raw, list) or not raw:
            raise GhAppError(
                f'tokenSource "app": App {self._signer.app_id} has no installations — '
                "install the app first (App settings → Install App), then restart")
        self._by_login = {
            i["account"]["login"].lower(): i["id"]
            for i in raw if (i.get("account") or {}).get("login")
        }

    def refresh(self) -> None:
        self.load()

    def installation_for(self, owner: str) -> int | None:
        return self._by_login.get(owner.lower())


class AppTokenSource:
    """Caches an installation token until close to expiry; mirrors Plotroom."""

    def __init__(self, signer: AppJwtSigner, installation_id: int, app_rest: AppRest,
                 clock: Callable[[], float] = time.time) -> None:
        self._signer = signer
        self._id = installation_id
        self._rest = app_rest
        self._clock = clock
        self._cached: tuple[str, float] | None = None  # (token, expires_at_epoch)

    def get(self) -> str:
        if self._cached and self._cached[1] - self._clock() > REFRESH_MARGIN_S:
            return self._cached[0]
        return self._mint()

    def refresh(self) -> str:
        self._cached = None
        return self._mint()

    def _mint(self) -> str:
        jwt = self._signer.mint()
        body = self._rest("POST", f"/app/installations/{self._id}/access_tokens", jwt)
        token = (body or {}).get("token")
        expires_at = (body or {}).get("expires_at")
        if not token or not expires_at:
            raise GhAppError("GitHub App: access_tokens response missing token/expires_at")
        self._cached = (token, _parse_expires(expires_at))
        return token
