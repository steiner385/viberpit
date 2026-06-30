# M-Track-9: GitHub App auth for PR polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate the population + eta-checks GraphQL poll path as GitHub App 4031757 via per-installation tokens (mimicking Plotroom's signer + installation registry + per-owner routing), falling back to the user `gh` token when unconfigured or an owner is uncovered.

**Architecture:** A new pure/sync `server/github/app_auth.py` ports Plotroom's `AppJwtSigner` (RS256 JWT via `cryptography`), `InstallationRegistry` (owner→installation), and `AppTokenSource` (cached installation tokens via `httpx`), behind a `token_for_owner(owner) -> str | None` facade. `gitrepos._run_gh` gains an optional `token=` kwarg (sets `GH_TOKEN`); the per-repo / per-owner-grouped poll calls route the owner's token in. Sync throughout (the poll runs in `asyncio.to_thread`).

**Tech Stack:** Python 3.11 / `cryptography` (RS256) / `httpx` (REST) / pytest.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-30-github-app-auth-for-polling-design.md`.
- **Mimic Plotroom verbatim** (`pr-dashboard/server/auth.ts`): `JWT_BACKDATE_S=60`, `JWT_TTL_S=540`, `TOKEN_REFRESH_MARGIN_MS=5*60_000` (300s); JWT header `{"alg":"RS256","typ":"JWT"}`, payload `{"iat": nowS-60, "exp": nowS+540, "iss": str(appId)}`; REST base `https://api.github.com`; `GET /app/installations`, `POST /app/installations/{id}/access_tokens`; headers `accept: application/vnd.github+json`, `user-agent: ccswitch`. Error wording mirrors Plotroom (e.g. "App {id} has no installations — install the app first").
- **No secret in logs/errors:** never log/format the token (`ghs_…`), the JWT, or the PEM. App-REST errors carry `method + path + HTTP status` only.
- **Credential source:** `CCSWITCH_GITHUB_APP_ID`, `CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH` (`0600` PEM), optional `CCSWITCH_GITHUB_APP_INSTALLATION_ID`. Unset → `token_for_owner` returns `None` → user `gh` token (no behaviour change).
- **Scope:** poll path only (`_default_query`, `_default_eta_query`). Other `gh` calls unchanged.
- **Per-owner routing:** each repo's poll uses its owner's installation token; uncovered owner / mint error → `None` → fallback (warn once per owner per process).
- **Sync + locked:** module singleton (signer, registry, per-installation sources) guarded by a `threading.Lock`; the poll runs in a worker thread.
- **No signature-change fan-out:** `_run_gh` gains an *optional* kwarg; existing callers unaffected. `place_pr_on_track`-style whole-suite caution doesn't apply, but `_run_gh` is a shared helper — run the whole backend suite.
- **Backend gate:** `ruff` + `mypy` + `pytest`, via `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/42659d9c/.venv/bin/python`; auth tests prefix `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.

---

### Task 1: Settings + `AppJwtSigner` (RS256 JWT mint)

**Files:**
- Create: `server/github/app_auth.py`
- Modify: `server/config.py` (both the `BaseSettings` class ~12-60 and the `@dataclasses.dataclass Settings` variant ~70+)
- Test: `server/tests/test_github_app_auth.py`

**Interfaces:**
- Produces: `mint_jwt(app_id: int, private_key_pem: str, now: float) -> str`; `class AppJwtSigner(app_id: int, private_key_pem: str, clock: Callable[[], float] = time.time)` with `.app_id` and `.mint() -> str`; settings `github_app_id`, `github_app_installation_id`, `github_app_private_key_path`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_github_app_auth.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -q`
Expected: FAIL — `server.github.app_auth` does not exist.

- [ ] **Step 3: Implement `app_auth.py` (signer only, for now)**

```python
# server/github/app_auth.py
"""GitHub App installation-token auth for the PR poller — a Python port of
Plotroom's server/auth.ts (AppJwtSigner + InstallationRegistry + AppTokenSource).
Sync (runs inside the poll's asyncio.to_thread); never logs the token/JWT/key."""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("server.github.app_auth")

JWT_BACKDATE_S = 60
JWT_TTL_S = 540


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_jwt(app_id: int, private_key_pem: str, now: float) -> str:
    """RS256 App JWT over b64url(header).b64url(payload), signed with the PEM key."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    nows = int(now)
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(
        {"iat": nows - JWT_BACKDATE_S, "exp": nows + JWT_TTL_S, "iss": str(app_id)}
    ).encode())
    signing_input = f"{head}.{payload}".encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{head}.{payload}.{_b64url(sig)}"


class AppJwtSigner:
    """Loads/validates the PEM ONCE; mints RS256 App JWTs."""

    def __init__(self, app_id: int, private_key_pem: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.app_id = app_id
        self._pem = private_key_pem
        # validate eagerly (raises on a bad PEM)
        serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        self._clock = clock

    def mint(self) -> str:
        return mint_jwt(self.app_id, self._pem, self._clock())
```

- [ ] **Step 4: Add the three settings fields**

In `server/config.py`, in the `BaseSettings` class (after `tailnet_allowlist`, ~line 14):
```python
        github_app_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_ID")
        github_app_installation_id: int | None = Field(
            default=None, alias="CCSWITCH_GITHUB_APP_INSTALLATION_ID")
        github_app_private_key_path: str | None = Field(
            default=None, alias="CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH")
```
In the `@dataclasses.dataclass Settings` variant (~line 70+), add the parallel fields:
```python
        github_app_id: int | None = dataclasses.field(
            default_factory=lambda: (
                int(os.environ["CCSWITCH_GITHUB_APP_ID"])
                if os.environ.get("CCSWITCH_GITHUB_APP_ID") else None))
        github_app_installation_id: int | None = dataclasses.field(
            default_factory=lambda: (
                int(os.environ["CCSWITCH_GITHUB_APP_INSTALLATION_ID"])
                if os.environ.get("CCSWITCH_GITHUB_APP_INSTALLATION_ID") else None))
        github_app_private_key_path: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH"))
```

- [ ] **Step 5: Run to verify pass + lint/type + commit**

```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -q
.venv/bin/python -m ruff check server/github/app_auth.py server/config.py
.venv/bin/python -m mypy server/github/app_auth.py   # ignore pre-existing ccswitch import-untyped
git add server/github/app_auth.py server/config.py server/tests/test_github_app_auth.py
git commit -m "feat(github-app): RS256 App JWT signer + CCSWITCH_GITHUB_APP_* settings"
```

---

### Task 2: `InstallationRegistry` + `AppTokenSource` + default REST

**Files:**
- Modify: `server/github/app_auth.py`
- Test: `server/tests/test_github_app_auth.py` (extend)

**Interfaces:**
- Consumes: `AppJwtSigner` (Task 1).
- Produces:
  - `class GhAppError(Exception)`.
  - `AppRest = Callable[[str, str, str], object]` — `(method, path, jwt) -> parsed JSON` (raises `GhAppError` on non-2xx).
  - `class InstallationRegistry(signer, app_rest, installation_id: int | None = None)` with `.load()`, `.installation_for(owner: str) -> int | None`, `.refresh()`.
  - `class AppTokenSource(signer, installation_id: int, app_rest, clock=time.time)` with `.get() -> str`, `.refresh() -> str`.
  - `REFRESH_MARGIN_S = 300`.
  - `default_app_rest(method, path, jwt)` (httpx).

- [ ] **Step 1: Write the failing tests**

```python
# add to server/tests/test_github_app_auth.py
import pytest
from server.github.app_auth import (
    InstallationRegistry, AppTokenSource, GhAppError, REFRESH_MARGIN_S,
)


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
            {"token": "ghs_AAA", "expires_at": "2023-11-14T22:18:00Z"},
    })
    # expires_at parses to an epoch; first get mints
    src = AppTokenSource(_signer(), 11, rest, clock=lambda: t[0])
    tok1 = src.get()
    assert tok1 == "ghs_AAA"
    assert len(rest.calls) == 1   # minted once
    tok2 = src.get()
    assert tok2 == "ghs_AAA" and len(rest.calls) == 1  # cached, no second mint
```

> **Implementer note:** pick `expires_at` so that, at `clock=1_700_000_000`, `expires_at - now > REFRESH_MARGIN_S` (cache holds). Compute it from the clock if clearer (e.g. ISO of `now + 3600`). The point: same token returned, `rest.calls` length unchanged on the second `get()`.

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -q`
Expected: FAIL — `InstallationRegistry`/`AppTokenSource` undefined.

- [ ] **Step 3: Implement the registry + token source + REST**

Append to `server/github/app_auth.py`:
```python
import datetime as _dt
from typing import Any

import httpx

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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -q`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
.venv/bin/python -m ruff check server/github/app_auth.py
.venv/bin/python -m mypy server/github/app_auth.py
git add server/github/app_auth.py server/tests/test_github_app_auth.py
git commit -m "feat(github-app): installation registry + cached per-installation token source"
```

---

### Task 3: `token_for_owner` facade (singleton, fallback, warn-once)

**Files:**
- Modify: `server/github/app_auth.py`
- Test: `server/tests/test_github_app_auth.py` (extend)

**Interfaces:**
- Consumes: `AppJwtSigner`, `InstallationRegistry`, `AppTokenSource`, `default_app_rest` (Tasks 1-2); `get_settings()` (`server.config`).
- Produces: `token_for_owner(owner: str) -> str | None`; `reset()` (test isolation). Module seams `_app_rest` and `_clock` (monkeypatchable).

- [ ] **Step 1: Write the failing tests**

```python
# add to server/tests/test_github_app_auth.py
from server.github import app_auth as A


def test_token_for_owner_none_when_unconfigured(monkeypatch):
    A.reset()
    monkeypatch.setattr(A, "_load_app_config", lambda: None)  # nothing configured
    assert A.token_for_owner("cairnea") is None


def test_token_for_owner_routes_per_owner(monkeypatch, tmp_path):
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
    assert A.token_for_owner("nobody") is None  # uncovered → fallback


def test_token_for_owner_swallows_mint_error(monkeypatch, caplog):
    A.reset()
    _key, pem = _keypair()
    rest = _FakeRest({("GET", "/app/installations"): GhAppError("boom")})
    monkeypatch.setattr(A, "_load_app_config", lambda: (4031757, None, pem))
    monkeypatch.setattr(A, "_app_rest", rest)
    assert A.token_for_owner("cairnea") is None
    assert "ghs_" not in caplog.text  # no token leaked
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -k token_for_owner -q`
Expected: FAIL — `token_for_owner`/`reset`/`_load_app_config` undefined.

- [ ] **Step 3: Implement the facade**

Append to `server/github/app_auth.py`:
```python
import threading

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
        pem = open(key_path, encoding="utf-8").read()
    except OSError:
        log.warning("github_app.private_key_unreadable")  # no path/secret in the msg
        return None
    return int(app_id), getattr(s, "github_app_installation_id", None), pem


def token_for_owner(owner: str) -> str | None:
    """Installation token for `owner`'s installation, or None → caller uses the user gh token."""
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
            inst = _registry.installation_for(owner)  # type: ignore[union-attr]
            if inst is None:
                if owner not in _warned_owners:
                    _warned_owners.add(owner)
                    log.warning("github_app.owner_no_installation owner=%s", owner)
                return None
            src = _sources.get(inst)
            if src is None:
                src = AppTokenSource(_signer, inst, _app_rest, clock=_clock)
                _sources[inst] = src
            return src.get()
    except Exception:
        log.warning("github_app.token_mint_failed", exc_info=True)  # never logs token/key
        return None
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_github_app_auth.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint/type + commit**

```bash
.venv/bin/python -m ruff check server/github/app_auth.py
.venv/bin/python -m mypy server/github/app_auth.py
git add server/github/app_auth.py server/tests/test_github_app_auth.py
git commit -m "feat(github-app): token_for_owner facade (per-owner routing, graceful fallback)"
```

---

### Task 4: `gitrepos._run_gh(token=…)`

**Files:**
- Modify: `src/ccswitch/gitrepos.py` (`_run_gh` ~70-81)
- Test: `server/tests/test_run_gh_token.py` (new)

**Interfaces:**
- Produces: `_run_gh(args: list[str], token: str | None = None) -> subprocess.CompletedProcess[str]` — when `token` is set, the subprocess env carries `GH_TOKEN=token` (in addition to the existing `GITHUB_TOKEN` strip).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_run_gh_token.py
from ccswitch import gitrepos


def test_run_gh_sets_gh_token_when_provided(monkeypatch):
    captured = {}
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class P: returncode = 0; stdout = "{}"; stderr = ""
        return P()
    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql", "-f", "query=x"], token="ghs_ABC")
    assert captured["env"]["GH_TOKEN"] == "ghs_ABC"


def test_run_gh_no_token_strips_github_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("GITHUB_TOKEN", "stale")
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class P: returncode = 0; stdout = "{}"; stderr = ""
        return P()
    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql"])
    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_run_gh_token.py -q`
Expected: FAIL — `_run_gh()` takes no `token` kwarg.

- [ ] **Step 3: Add the `token` kwarg**

In `src/ccswitch/gitrepos.py`, replace `_run_gh`:
```python
def _run_gh(args: list[str], token: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command. ``GITHUB_TOKEN`` is always stripped; when ``token`` is
    given (a GitHub App installation token), ``GH_TOKEN`` is set so gh uses it."""
    env = _gh_env()
    if token:
        env["GH_TOKEN"] = token
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise GhError("the `gh` CLI is not installed or not on PATH") from exc
```

- [ ] **Step 4: Run to verify pass + commit**

```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_run_gh_token.py -q
.venv/bin/python -m ruff check src/ccswitch/gitrepos.py
git add src/ccswitch/gitrepos.py server/tests/test_run_gh_token.py
git commit -m "feat(gitrepos): _run_gh optional token kwarg (GH_TOKEN passthrough)"
```

---

### Task 5: Route the poll path through the App token (per owner, eta grouped)

**Files:**
- Modify: `server/github/pr_population.py` (`_default_query` ~39-46, `_default_eta_query` ~47-56)
- Test: `server/tests/test_population_app_token.py` (new)

**Interfaces:**
- Consumes: `app_auth.token_for_owner` (Task 3); `gitrepos._run_gh(token=…)` (Task 4).
- Produces: `_default_query`/`_default_eta_query` route the owner's installation token into `_run_gh`; the eta query is grouped by owner and merged with original-index aliases.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_population_app_token.py
from server.github import pr_population as P


def test_default_query_passes_owner_token(monkeypatch):
    calls = {}
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: f"tok-{owner}")
    def fake_run(args, token=None):
        calls["token"] = token
        class R: returncode = 0; stdout = '{"data":{}}'; stderr = ""
        return R()
    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    P._default_query("cairnea/KinDash", retention_days=7)
    assert calls["token"] == "tok-cairnea"


def test_eta_query_grouped_by_owner_merges_with_global_aliases(monkeypatch):
    # two owners → two gh calls; aliases remapped to the original key order
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: f"tok-{owner}")
    seen = []
    def fake_run(args, token=None):
        seen.append(token)
        # echo a single-node response keyed r0 (the subset's first alias)
        class R: returncode = 0; stdout = '{"data":{"r0":{"pullRequest":{"number":1}}}}'; stderr = ""
        return R()
    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    keys = [("cairnea/KinDash", 1), ("steiner385/cc-account-switcher", 2)]
    resp = P._default_eta_query(keys)
    # one call per owner, each with that owner's token
    assert set(seen) == {"tok-cairnea", "tok-steiner385"}
    # subset alias r0 remapped: cairnea key idx 0 → r0, steiner key idx 1 → r1
    assert resp["data"]["r0"]["pullRequest"]["number"] == 1
    assert resp["data"]["r1"]["pullRequest"]["number"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_population_app_token.py -q`
Expected: FAIL — `_default_query` takes no token; `_default_eta_query` runs a single un-grouped call.

- [ ] **Step 3: Route the token in both poll functions**

In `server/github/pr_population.py`, add the import near the top (with the other `from server.github` / `ccswitch` imports):
```python
from server.github import app_auth
```
Replace `_default_query`:
```python
def _default_query(repo: str, retention_days: int) -> dict:
    query = build_population_query(repo, _merged_since(retention_days))
    token = app_auth.token_for_owner(repo.split("/", 1)[0])
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")
```
Replace `_default_eta_query` (group by owner; one gh call per owner; remap each subset's `r{j}` alias back to the original global index `r{global_i}`):
```python
def _default_eta_query(prs: list[tuple[str, int]]) -> dict:
    """Run build_eta_checks_query via gh, grouped by owner so each owner's PRs use
    that owner's App installation token. Responses are merged with aliases remapped
    to the original `prs` index, so parse_eta_checks_response(response, prs) is unchanged."""
    if not prs:
        return {}
    by_owner: dict[str, list[int]] = {}
    for i, (repo, _num) in enumerate(prs):
        by_owner.setdefault(repo.split("/", 1)[0], []).append(i)
    merged: dict = {}
    for owner, idxs in by_owner.items():
        subset = [prs[i] for i in idxs]
        query = build_eta_checks_query(subset)
        token = app_auth.token_for_owner(owner)
        proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
        if proc.returncode != 0:
            raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql (eta) failed")
        sub_data = (json.loads(proc.stdout or "{}").get("data") or {})
        for j, global_i in enumerate(idxs):
            node = sub_data.get(f"r{j}")
            if node is not None:
                merged[f"r{global_i}"] = node
    return {"data": merged}
```

- [ ] **Step 4: Run the new test + the population/eta tests it could affect**

```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  server/tests/test_population_app_token.py server/tests/test_pr_population.py \
  server/tests/test_population_eta_wired.py server/tests/test_eta_checks_query.py \
  server/tests/test_merge_group_checks.py -q
```
Expected: PASS. The eta-grouping must keep `parse_eta_checks_response`/`parse_merge_group_response` working (they read `r{i}` by original index — the remap preserves that).

- [ ] **Step 5: Lint/type + WHOLE backend suite (shared `_run_gh` helper changed)**

```bash
.venv/bin/python -m ruff check server/github/pr_population.py
.venv/bin/python -m mypy server/github/pr_population.py server/github/app_auth.py
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q
```
Expected: ruff/mypy clean; whole suite — no NEW failures vs `origin/master` baseline (known environmental: test_ws StaticPool, real-xvfb, real-daemon socket).

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_population.py server/tests/test_population_app_token.py
git commit -m "feat(pipeline): poll via GitHub App installation tokens, routed per owner"
```

---

## Self-Review

**Spec coverage:**
- `AppJwtSigner` (RS256, constants verbatim, one PEM read) → Task 1. ✅
- `InstallationRegistry` (owner→installation, case-insensitive, error wording, restrict-to) → Task 2. ✅
- `AppTokenSource` (cache + 5-min margin) → Task 2. ✅
- `token_for_owner` facade (singleton, lock, per-owner routing, warn-once, graceful fallback, no secret logs) → Task 3. ✅
- Settings (3 vars, both Settings variants) → Task 1. ✅
- `_run_gh(token=)` GH_TOKEN passthrough → Task 4. ✅
- Poll routing per owner; eta query grouped + alias-remapped → Task 5. ✅
- Fallback to user token when unconfigured/uncovered/mint-error → Tasks 3 (None) + 5 (`token=None` path). ✅
- Whole-suite for the shared `_run_gh` change → Task 5 Step 5. ✅
- Secret hygiene (no token/JWT/key in logs; ephemeral test keypair) → Tasks 1-3 tests assert no `ghs_` in caplog. ✅

**Placeholder scan:** Task 2 Step 1's implementer note pins the `expires_at` choice (compute from clock so the cache holds) — concrete guidance, the assertions are exact. No bare TODO/TBD.

**Type consistency:** `mint_jwt(app_id, pem, now)`, `AppJwtSigner(app_id, pem, clock)`, `InstallationRegistry(signer, app_rest, installation_id)`, `AppTokenSource(signer, installation_id, app_rest, clock)`, `token_for_owner(owner) -> str | None`, `default_app_rest(method, path, jwt)`, `_run_gh(args, token=None)` are defined once and consumed identically across tasks; `REFRESH_MARGIN_S=300`, `JWT_BACKDATE_S=60`, `JWT_TTL_S=540` are the single constants; the eta alias remap (`r{j}`→`r{global_i}`) keeps `parse_eta_checks_response(response, prs)` reading by original index.
