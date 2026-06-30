# M-Track-9: GitHub App auth for PR polling — Design

**Date:** 2026-06-30
**Status:** Approved (design); pending user review → implementation plan
**Context:** ccswitch's PR-population poller authenticates as the host `gh` **user** (shared 5,000 req/hr)
over GraphQL. Reuse Plotroom's existing **GitHub App 4031757** to poll via short-lived installation
tokens (15,000 req/hr per installation). Polling is already GraphQL — this swaps only the credential.

---

## 1. Goal

Authenticate the **population + eta-checks GraphQL poll path** as GitHub App 4031757 (the App Plotroom
already uses), minting and caching installation tokens, so frequent polling no longer competes for the
user's 5k/hr limit. Falls back to today's user-`gh` path when the App isn't configured. **Poll path
only** for now (decided); other `gh` calls (branch list, default-branch, PR fix actions) stay on the
user token — expanding to those is a later milestone.

## 2. Background (verified)

- Plotroom (`pr-dashboard/config.json`): `tokenSource: "app"`, `app.appId: 4031757`, private key inline.
  Its `server/auth.ts` mints a JWT (RS256, `iss=appId`, backdated `iat`, `exp` ≈ +9 min) → resolves the
  installation (`GET /app/installations`, or a configured `installationId`) → `POST
  /app/installations/{id}/access_tokens` → `{token, expires_at}`, cached until a refresh margin.
- ccswitch `_run_gh` (`src/ccswitch/gitrepos.py`) strips `GITHUB_TOKEN` so `gh` uses the keyring user
  login. The poll queries run `gh api graphql` via `_run_gh`.
- Deps present: **`httpx` 0.28** (REST mint calls) and **`cryptography`** (RS256 JWT signing — no new
  dependency, no PyJWT).

## 3. Decisions (locked during brainstorming)

1. **Credential storage:** a `0600` PEM file referenced by env, NOT plaintext-inline (Plotroom's way) and
   NOT ccswitch's envelope crypto (overkill for App infra config):
   - `CCSWITCH_GITHUB_APP_ID` (e.g. `4031757`)
   - `CCSWITCH_GITHUB_APP_INSTALLATION_ID` (optional — auto-resolved if a single installation)
   - `CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH` → a `0600` file holding App 4031757's PEM (copied out of
     Plotroom's `config.json`).
2. **Scope:** only the population + eta GraphQL poll path uses the App token. Other `gh` calls unchanged.
3. **Graceful fallback:** App not configured, or any mint failure → fall back to the user-`gh` path
   (log a warning, redaction-safe). The board never goes dark over an auth-config gap.

## 4. Architecture

### 4.1 `server/github/app_auth.py` — token minting (sync; runs inside the poll thread)
The poll queries run via `asyncio.to_thread`, so a **synchronous** provider (httpx `Client`) avoids
async/sync friction. Module-level singleton + `threading.Lock` around refresh.

- `_mint_jwt(app_id: int, private_key_pem: str, now: float) -> str` — header `{"alg":"RS256","typ":"JWT"}`,
  payload `{"iat": int(now)-60, "exp": int(now)+540, "iss": str(app_id)}`, base64url-encoded, signed with
  the RSA key via `cryptography` (`load_pem_private_key` → `sign(..., PKCS1v15(), SHA256())`).
- `class AppTokenProvider`: fields `app_id`, `private_key_pem`, `installation_id | None`, injectable
  `http` (callable like `httpx.request`) and `clock`. Holds `_cached: {token, expires_at} | None`.
  - `token() -> str` — return cached token while `expires_at - now > REFRESH_MARGIN_S (300)`, else
    `refresh()`.
  - `refresh()` — mint JWT → if `installation_id` unset, `GET /app/installations` (use the sole
    installation; if 0 or >1 and unset, raise a clear error) → `POST
    /app/installations/{id}/access_tokens` (Bearer JWT) → cache `{token, expires_at}` (parse GitHub's
    `expires_at` ISO).
- `get_poll_token() -> str | None` — module entry point used by the poller: builds the singleton from
  `get_settings()` (returns `None` if `github_app_id`/key path absent), calls `.token()`, and returns the
  token; on ANY exception logs a redaction-safe warning (`github_app.token_mint_failed`) and returns
  `None` (→ caller falls back). **Never logs the token or key.**

### 4.2 Settings — `server/config.py`
Add three pydantic fields:
```
github_app_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_ID")
github_app_installation_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_INSTALLATION_ID")
github_app_private_key_path: str | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH")
```
(The dataclass `Settings` variant for remote/daemon mode gets the same three via `os.environ.get`.)

### 4.3 `gitrepos._run_gh` — optional installation token
`src/ccswitch/gitrepos.py`: `_run_gh(args, token: str | None = None)`. When `token` is provided, run the
subprocess with `GH_TOKEN=<token>` in the env (and the existing `GITHUB_TOKEN`-strip still applies, so
only the App token is present; `gh` prefers `GH_TOKEN`). When `token is None`, behaviour is unchanged
(user keyring login). A `GhError` is unchanged.

### 4.4 Poll path wiring — `server/github/pr_population.py`
`_default_query` and `_default_eta_query` (both sync, run in `to_thread`): call
`app_auth.get_poll_token()` and pass `token=` to `gitrepos._run_gh`. `None` → unchanged user-token call.
No change to the GraphQL documents or response parsing.

## 5. Data flow

```
poll cycle (to_thread)
  → app_auth.get_poll_token()  [singleton + lock]
       cached & fresh? → reuse
       else refresh: mint JWT (cryptography RS256) → GET /app/installations → POST access_tokens (httpx) → cache
  → gitrepos._run_gh(["api","graphql","-f",query], token=<ghs_… or None>)
       token set → GH_TOKEN=token (App installation, 15k/hr); None → user keyring (fallback)
  → same GraphQL parse as today
```

## 6. Error handling & edge cases

- **App not configured** (`github_app_id` or key path unset) → `get_poll_token()` returns `None` → user
  token path. No error surfaced. This is the default until the operator sets the env + key file.
- **Mint/exchange failure** (bad key, network, GitHub 5xx, 0/many installations with none configured) →
  caught in `get_poll_token`, warning logged (no secret material), returns `None` → fallback. A poll
  never crashes over auth.
- **Token expiry / clock skew** — refresh margin 300s; JWT `iat` backdated 60s (GitHub's clock-skew
  guard, mirrors Plotroom).
- **Secret hygiene:** the PEM file is `0600` and **git-ignored** (add the canonical path / `*.pem` to
  `.gitignore` if a default path is used); the token (`ghs_…`) and key are NEVER logged (the obs
  redaction filter also scrubs token-shaped literals — don't rely on it, just don't log them).
- **Concurrency:** the singleton's `refresh()` is guarded by a `threading.Lock` (the poll runs in a
  worker thread; one refresh at a time).

## 7. Testing (TDD)

- `_mint_jwt`: header/payload claims (`iss=appId`, `iat` backdated, `exp`=iat+~9min); the signature
  verifies against the RSA **public** key (generate an ephemeral RSA keypair in-test — no real App key).
- `AppTokenProvider` with an **injected `http` fn + clock** (no live GitHub): mints once and caches;
  reuses within margin; refreshes when within `REFRESH_MARGIN_S`; resolves a sole installation; errors
  clearly on 0/many installations when `installation_id` unset.
- `get_poll_token`: returns `None` when settings unset; returns `None` + warning on mint exception
  (fallback); returns the token when configured. Assert no token/key string appears in caplog.
- `gitrepos._run_gh(token=…)`: subprocess env carries `GH_TOKEN=<token>`; `token=None` path leaves no
  `GH_TOKEN` and strips `GITHUB_TOKEN` (existing behaviour) — assert via an injected/echoed env.
- `pr_population` poll path: when `get_poll_token` returns a token, `_run_gh` is called with `token=`;
  when `None`, called without — using a stub.
- Full backend suite green (no signature-change fan-out: `_run_gh` gains an optional kwarg; existing
  callers unaffected).

## 8. Files touched

**New**
- `server/github/app_auth.py` (+ test `server/tests/test_github_app_auth.py`).

**Modified**
- `server/config.py` — three `CCSWITCH_GITHUB_APP_*` settings (both Settings variants).
- `src/ccswitch/gitrepos.py` — `_run_gh(token=…)` optional kwarg (+ test).
- `server/github/pr_population.py` — poll path passes `app_auth.get_poll_token()` to `_run_gh` (+ test).
- `.gitignore` — ignore the App PEM if a default in-repo/`.dev` path is documented (keys never committed).

## 9. Operational note (not code)

After merge, the operator (one-time): copy App 4031757's private key from `pr-dashboard/config.json`
into a `0600` PEM file, and set `CCSWITCH_GITHUB_APP_ID=4031757`,
`CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH=<that file>`, and (recommended) `CCSWITCH_GITHUB_APP_INSTALLATION_ID`
in the deploy env. Until then, polling transparently uses the user token (no behaviour change).

## 10. Risks

- **Private key handling** — `0600` file, git-ignored, never logged; the env points at it. The plan must
  not commit a key or print it in tests (tests use an ephemeral in-test keypair).
- **Installation scope** — App 4031757's installation must cover the watched repos (it polls Plotroom's
  repos today, which overlap). If a watched repo isn't in the installation, that repo's poll 404s under
  the App token; the fallback covers the not-configured case, but a misscoped install would need the
  per-repo error to degrade gracefully — log + skip that repo (population already best-effort per repo).
- **`gh` GH_TOKEN precedence** — confirm `gh` uses `GH_TOKEN` over a keyring login (it does); the
  `_run_gh(token=)` path sets it explicitly.
- **No signature-change fan-out** — `_run_gh` gains an optional kwarg; the poll path is the only new
  caller. Whole-suite run still recommended (it's the `gitrepos` shared helper).
