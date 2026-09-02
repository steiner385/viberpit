"""GitHub App installation-token auth for the PR poller — a Python port of
Plotroom's server/auth.ts (AppJwtSigner + InstallationRegistry + AppTokenSource).
Sync (runs inside the poll's asyncio.to_thread); never logs the token/JWT/key."""
from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import threading
import time
from typing import Any, Callable, cast

import httpx
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
        # load_pem_private_key validates the PEM is parseable (raises on a bad PEM);
        # cast is only for the type-checker; a non-RSA key would fail later at .sign() time
        cast(RSAPrivateKey, serialization.load_pem_private_key(private_key_pem.encode(), password=None))
        self._clock = clock

    def mint(self) -> str:
        return mint_jwt(self.app_id, self._pem, self._clock())


# ---------------------------------------------------------------------------
# Task 2: GhAppError, AppRest, default_app_rest, InstallationRegistry,
#          AppTokenSource, REFRESH_MARGIN_S
# ---------------------------------------------------------------------------

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

    def owners(self) -> list[str]:
        """Every owner login with an installation (lowercased, load order)."""
        return list(self._by_login)


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


# ---------------------------------------------------------------------------
# Task 3: token_for_owner facade (singleton, fallback, warn-once)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_app_rest: AppRest = default_app_rest        # monkeypatchable seam
_clock: Callable[[], float] = time.time       # monkeypatchable seam

_signer: AppJwtSigner | None = None
_registry: InstallationRegistry | None = None
_sources: dict[int, AppTokenSource] = {}
_registry_loaded = False
_warned_owners: set[str] = set()


def reset() -> None:
    """Test isolation: drop all cached state."""
    global _signer, _registry, _registry_loaded
    with _lock:
        _signer = None
        _registry = None
        _registry_loaded = False
        _sources.clear()
        _warned_owners.clear()


def _load_app_config() -> tuple[int, int | None, str] | None:
    """(app_id, installation_id, private_key_pem) or None when not configured."""
    from server.config import get_settings
    s = get_settings()
    app_id = getattr(s, "github_app_id", None)
    key_path = getattr(s, "github_app_private_key_path", None)
    if not app_id or not key_path:
        return None
    try:
        with open(key_path, encoding="utf-8") as fh:
            pem = fh.read()
    except OSError:
        log.warning("github_app.private_key_unreadable")  # no path/secret in the msg
        return None
    return int(app_id), getattr(s, "github_app_installation_id", None), pem


def _loaded_signer_and_registry(
    cfg: tuple[int, int | None, str],
) -> tuple[AppJwtSigner, InstallationRegistry]:
    """The singleton signer + registry, built and loaded on first use.

    Call under ``_lock``. Shared by :func:`token_for_owner` and
    :func:`installation_owners` so the ``/app/installations`` load happens once
    for both, rather than each facade bootstrapping a registry of its own.
    """
    global _signer, _registry, _registry_loaded
    app_id, inst_id_pin, pem = cfg
    if _signer is None:
        _signer = AppJwtSigner(app_id, pem, clock=_clock)
        _registry = InstallationRegistry(_signer, _app_rest, installation_id=inst_id_pin)
        _registry_loaded = False
    if not _registry_loaded:
        _registry.load()  # type: ignore[union-attr]
        _registry_loaded = True
    return _signer, cast(InstallationRegistry, _registry)


def register_gitrepos_providers() -> None:
    """Point ``ccswitch.gitrepos`` at this App for both token and owner lookup.

    ONE registration site for both seams, called by every process that talks to
    GitHub — the daemon (``server.ipc.serve``, where clone/fetch run) and the edge
    (``server.app``, where repo/branch listing runs). Registering them separately
    per process is how the edge ended up with the token seam only, silently
    listing repos as the machine's ambient gh user.

    Config is read at call time, not registration time — both processes register
    during startup, so an App configured later (or not at all) is still handled.
    The owners provider returns None when no App is configured, which keeps repo
    listing on the viewer path instead of hard-failing an App-less deployment.
    """
    from ccswitch.gitrepos import set_installations_provider, set_token_provider

    def _owners() -> list[str] | None:
        return None if _load_app_config() is None else installation_owners()

    set_token_provider(lambda owner: token_for_owner(owner))
    set_installations_provider(_owners)


def installation_owners() -> list[str]:
    """Owner logins this App is installed on; empty when unconfigured or broken.

    Companion to :func:`token_for_owner` for callers that must DISCOVER owners
    rather than resolve a token for a known one — repo enumeration, which has no
    owner to start from. Reuses the same ``InstallationRegistry`` (and its cached
    ``/app/installations`` load) rather than issuing a second listing of its own.

    Returns [] rather than raising: the caller decides whether an empty list is
    fatal, and this must never take down a request path on a GitHub blip.
    """
    cfg = _load_app_config()
    if cfg is None:
        return []
    try:
        with _lock:
            _, registry = _loaded_signer_and_registry(cfg)
            return registry.owners()
    except Exception:
        log.warning("github_app.installations_list_failed", exc_info=True)  # never logs key
        return []


def token_for_owner(owner: str) -> str | None:
    """Installation token for `owner`'s installation, or None → caller uses the user gh token."""
    cfg = _load_app_config()
    if cfg is None:
        return None
    try:
        with _lock:
            signer, registry = _loaded_signer_and_registry(cfg)
            inst = registry.installation_for(owner)
            if inst is None:
                if owner not in _warned_owners:
                    _warned_owners.add(owner)
                    log.warning("github_app.owner_no_installation owner=%s", owner)
                return None
            src = _sources.get(inst)
            if src is None:
                # _app_rest is captured per installation at first construction; tests reset() before patching.
                src = AppTokenSource(signer, inst, _app_rest, clock=_clock)
                _sources[inst] = src
            return src.get()
    except Exception:
        log.warning("github_app.token_mint_failed", exc_info=True)  # never logs token/key
        return None
