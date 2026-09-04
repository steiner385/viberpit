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

    def installation_ids(self) -> list[int]:
        """Every distinct installation id this App is installed on (issue #912).

        Repo enumeration iterates installations rather than resolving one owner, so
        it needs the ids without knowing any owner up front."""
        return sorted(set(self._by_login.values()))


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


# ---------------------------------------------------------------------------
# Issue #912: installation-repo enumeration + shared provider registration
# ---------------------------------------------------------------------------

# Hard bound on /installation/repositories pages (100 repos/page) so a
# misbehaving API can never spin forever; far above any real installation.
_MAX_REPO_PAGES = 100


def _list_installation_repos(token: str, *, per_page: int = 100) -> list[dict]:
    """All repo objects accessible to one installation, via its installation token.

    ``/installation/repositories`` is authenticated with the *installation* token
    (passed as the bearer credential — the ``_app_rest`` seam sends it as
    ``Authorization: Bearer``, which GitHub accepts for installation tokens). Paged
    with ``page``/``per_page`` and stopped when a short page arrives."""
    out: list[dict] = []
    for page in range(1, _MAX_REPO_PAGES + 1):
        body = _app_rest(
            "GET", f"/installation/repositories?per_page={per_page}&page={page}", token)
        repos = (body or {}).get("repositories") or []
        out.extend(repos)
        if len(repos) < per_page:
            break
    return out


def installation_repos() -> list[dict] | None:
    """Raw REST repo objects for every installation, or None when no App is configured.

    Feeds ``gitrepos.list_accessible_repos`` on the App path (the repo chooser). Returns
    None when no App is configured, so standalone CLI usage keeps the viewer path. When
    an App IS configured but enumeration fails, raises ``gitrepos.GhError`` so the chooser
    fails loudly rather than degrading to the ambient user credential (issue #912)."""
    from ccswitch.gitrepos import GhError

    cfg = _load_app_config()
    if cfg is None:
        return None
    app_id, inst_id_pin, pem = cfg
    try:
        with _lock:
            global _signer, _registry, _registry_loaded
            if _signer is None:
                _signer = AppJwtSigner(app_id, pem, clock=_clock)
                _registry = InstallationRegistry(_signer, _app_rest, installation_id=inst_id_pin)
                _registry_loaded = False
            if not _registry_loaded:
                _registry.load()  # type: ignore[union-attr]
                _registry_loaded = True
            repos: list[dict] = []
            for inst in _registry.installation_ids():  # type: ignore[union-attr]
                src = _sources.get(inst)
                if src is None:
                    src = AppTokenSource(_signer, inst, _app_rest, clock=_clock)
                    _sources[inst] = src
                repos.extend(_list_installation_repos(src.get()))
            return repos
    except Exception as exc:
        # exc is a GhAppError / httpx error whose str() carries no token; log with
        # exc_info (redacting filter scrubs any secret) and re-raise as GhError so
        # the chooser surfaces a loud 502 rather than an empty list.
        log.warning("github_app.installation_repos_failed", exc_info=True)
        raise GhError(f"GitHub App repo enumeration failed: {exc}") from exc


def register_git_providers() -> None:
    """Point gitrepos at the pr-dashy App for BOTH git auth and repo enumeration.

    Registered from every process that lists or clones repos — the daemon
    (``server/ipc/serve.py``, where ``ensure_clone`` runs), the edge
    (``server/app.py``, where the repo/branch chooser runs), and the deploy
    controller (``server/deploy/serve.py``). Issue #912 found the edge never
    registered a provider, so the chooser silently used the ambient gh user and
    blanked when that credential expired. ``token_for_owner`` mints a per-owner
    installation token for git clone/fetch; ``installation_repos`` enumerates the
    repos the App can reach. Both are read through the module attributes so the App
    config is consulted at call time, and both no-op to ambient/viewer when no App
    is configured."""
    from ccswitch import gitrepos

    gitrepos.set_token_provider(lambda owner: token_for_owner(owner))
    gitrepos.set_installations_provider(installation_repos)
