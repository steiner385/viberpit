# M-Track-9: GitHub App auth for PR polling (mimic Plotroom) — Design

**Date:** 2026-06-30
**Status:** Approved (design, revised to mimic Plotroom faithfully); pending user review → plan
**Context:** ccswitch's PR poller authenticates as the host `gh` **user** (shared 5,000 req/hr) over
GraphQL. Reuse Plotroom's existing **GitHub App 4031757** with the **same mechanism Plotroom uses** —
a shared JWT signer, an installation registry (owner→installation), and per-installation tokens —
so polling runs on installation tokens (15,000 req/hr per installation).

---

## 1. Goal

Port Plotroom's `server/auth.ts` App-token machinery into ccswitch (Python) **faithfully**, and route the
**population + eta-checks GraphQL poll path** through it: each repo's poll uses the installation token for
**that repo's owner** (cairnea vs steiner385 are distinct installations of App 4031757). Falls back to the
user-`gh` token when the App isn't configured or an owner has no installation. **Poll path only** for now
(other `gh` calls unchanged — expand later, per decision).

## 2. What we mimic (from `pr-dashboard/server/auth.ts`, read in full)

Three pieces, ported 1:1 (constants, JWT shape, REST paths, error wording):

- **`AppJwtSigner`** — PEM read & validated **once** at construction; `mint()` builds an RS256 JWT:
  `header=b64url({"alg":"RS256","typ":"JWT"})`, `payload=b64url({"iat": nowS-60, "exp": nowS+540,
  "iss": str(appId)})`, `sig=b64url(RSA-SHA256 over "head.payload")`. Constants verbatim:
  `JWT_BACKDATE_S=60`, `JWT_TTL_S=540`.
- **`InstallationRegistry`** — `load()` mints a JWT and `GET /app/installations` → builds a
  case-insensitive `login → {id}` map; `installationFor(owner) -> id | None`; `refresh()` re-loads (new
  installs without restart). If pinned to one `installationId`, resolves just `GET
  /app/installations/{id}`. Same "no installations — install the app first" / multi-install error wording.
- **`AppTokenSource`** (one per installation) — caches `{token, expiresAt}`; `get()` returns the cached
  token while `expiresAt - now > TOKEN_REFRESH_MARGIN_MS (5*60_000)`, else mints: JWT → `POST
  /app/installations/{id}/access_tokens` (Bearer JWT) → cache `{token, Date.parse(expires_at)}`. Shared
  in-flight mint so concurrent callers exec once. REST base derived from the GraphQL api URL
  (`…/graphql` → `https://api.github.com`), exactly like Plotroom's `deriveRestBase`.

Plotroom's `GithubClient` uses the token as a `Bearer` on its **own** `fetch` GraphQL calls and routes
**one client per installation** keyed by owner (`routedClient(owner)`), skipping owners with no
installation (logged once). ccswitch instead issues GraphQL via the **`gh` CLI per repo**, so the
adaptation is: resolve the token for the repo's owner and hand it to `gh` via `GH_TOKEN`.

## 3. Decisions (locked)

1. **Credential storage** — `0600` PEM file via env, not plaintext-inline (Plotroom) nor envelope crypto:
   `CCSWITCH_GITHUB_APP_ID`, `CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH`, optional
   `CCSWITCH_GITHUB_APP_INSTALLATION_ID` (pins a single installation; otherwise the registry maps all).
2. **Scope** — population + eta GraphQL poll path only. Other `gh` calls unchanged.
3. **Per-owner routing** (faithful to Plotroom) — each repo's poll uses its owner's installation token;
   an owner with no installation, or App-not-configured, falls back to the user `gh` token (logged once
   per owner, like Plotroom's invisible-owner warning).
4. **Sync implementation** — the poll runs in `asyncio.to_thread`, so the signer/registry/token-source
   use a **synchronous** `httpx.Client`; a module singleton + `threading.Lock` guards mint/registry load.

## 4. Architecture — `server/github/app_auth.py` (Python port)

- `mint_jwt(app_id, private_key_pem, now) -> str` — RS256 via **`cryptography`** (`load_pem_private_key`
  → `sign(data, PKCS1v15(), SHA256())`), base64url header/payload/sig. (No PyJWT; `cryptography` is a dep.)
- `class AppJwtSigner` — loads/validates the PEM once; `mint()`.
- `class InstallationRegistry(signer, http, installation_id=None)` — `load()` / `installation_for(owner)`
  / `refresh()`; case-insensitive owner map; the verbatim error wording.
- `class AppTokenSource(signer, installation_id, http, clock)` — `get()` with the 5-min refresh margin +
  in-flight de-dupe; `refresh()`.
- **Module facade** `token_for_owner(owner: str) -> str | None`:
  - Build (once, lazily, lock-guarded) the shared `AppJwtSigner` + `InstallationRegistry` from
    `get_settings()`; return `None` immediately if `github_app_id`/key path unset (→ caller uses user gh).
  - `inst_id = registry.installation_for(owner)`; `None` → log once (`github_app.owner_no_installation
    owner=…`) and return `None` (fallback).
  - Lazily create + cache an `AppTokenSource` per `inst_id`; return `.get()`.
  - **Any** exception (bad key, network, GitHub 5xx) → log a redaction-safe warning
    (`github_app.token_mint_failed`) and return `None` (→ fallback). Never logs token/JWT/key.
- `apiUrl` is `https://api.github.com/graphql`; REST base `https://api.github.com` (the `deriveRestBase`
  port). Headers mirror Plotroom: `accept: application/vnd.github+json`, `user-agent: ccswitch`.

### Settings (`server/config.py`) — both Settings variants
```
github_app_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_ID")
github_app_installation_id: int | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_INSTALLATION_ID")
github_app_private_key_path: str | None = Field(default=None, alias="CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH")
```

### `gitrepos._run_gh(args, token: str | None = None)` (`src/ccswitch/gitrepos.py`)
`token` provided → subprocess env sets `GH_TOKEN=<token>` (the existing `GITHUB_TOKEN` strip still
applies, so only the App token is present; `gh` prefers `GH_TOKEN`). `token=None` → unchanged.

### Poll wiring (`server/github/pr_population.py`)
`_default_query(repo)` / `_default_eta_query(prs)`: derive owner (`repo.split("/")[0]`; for the eta query,
group by owner / use the first key's owner — see edge cases), call `app_auth.token_for_owner(owner)`, pass
`token=` to `_run_gh`. `None` → unchanged user-token call. No change to the GraphQL docs or parsing.

## 5. Data flow

```
poll repo "owner/name" (to_thread)
  → app_auth.token_for_owner("owner")
       not configured? → None
       registry.installation_for(owner) → inst_id  (load() once; GET /app/installations w/ JWT)
       AppTokenSource[inst_id].get() → cached or mint (JWT → POST access_tokens) → ghs_… token
       owner uncovered / mint error → log once → None
  → gitrepos._run_gh(["api","graphql","-f",query], token=ghs_… or None)
       token → GH_TOKEN (App installation, 15k/hr) ; None → user keyring (fallback)
  → same GraphQL parse
```

## 6. Error handling & edge cases

- **App not configured** → `token_for_owner` returns `None` → user token. Default state until the operator
  sets the env + key file. No behaviour change.
- **Owner has no installation** → `None` + one-time warning → that repo polls on the user token (graceful;
  mirrors Plotroom's skip-with-warning, but ccswitch falls back rather than skipping the repo entirely).
- **Mint/registry failure** (bad key, network, 5xx, 0/many installations unconfigured) → caught in the
  facade, warning (no secret), `None` → fallback. A poll never crashes over auth.
- **eta-checks query spans multiple owners** (it batches PRs across repos): group `open_keys` by owner and
  run one `gh` call per owner-group with that owner's token (mirrors Plotroom's per-owner requests), OR —
  simpler first cut — if all open keys share one owner use its token, else fall back to user token for the
  mixed batch. **Decision: group the eta query by owner** (one `_run_gh` per owner subset) to stay on App
  tokens; documented, tested.
- **Token expiry / clock skew** — 5-min refresh margin; JWT `iat` backdated 60s (verbatim).
- **Secret hygiene** — PEM `0600`, git-ignored; token/JWT/key never logged (obs redaction also scrubs
  token-shaped literals, but don't rely on it).
- **Concurrency** — module singleton (signer, registry, per-inst sources) guarded by `threading.Lock`;
  in-flight mint de-dupe per source (Plotroom's pattern).

## 7. Testing (TDD) — all with an injected `http` fn + clock; no live GitHub; ephemeral in-test RSA keypair

- `mint_jwt`/`AppJwtSigner`: header/payload claims (`iss`, `iat=now-60`, `exp=now+540`); signature
  verifies with the public key; invalid PEM → clear error.
- `InstallationRegistry`: `load()` maps owner→id (case-insensitive); `installation_for` hit/miss;
  `installationId`-pinned path; 0-install and multi-install error wording.
- `AppTokenSource`: mint+cache; reuse within margin; refresh at margin; in-flight de-dupe; cache uses
  `expires_at`.
- `token_for_owner`: `None` when unconfigured; per-owner routing returns the right installation's token;
  uncovered owner → `None` + one warning; mint exception → `None` + warning; **no token/key in caplog**.
- `gitrepos._run_gh(token=…)`: subprocess env carries `GH_TOKEN`; `token=None` leaves none + strips
  `GITHUB_TOKEN`.
- `pr_population`: per-repo poll passes the owner's token; eta query grouped by owner; `None` → user path.
- Whole backend suite (the `_run_gh` optional kwarg is the only shared-helper change; existing callers
  unaffected).

## 8. Files touched

**New:** `server/github/app_auth.py` (+ `server/tests/test_github_app_auth.py`).
**Modified:** `server/config.py` (3 settings, both variants); `src/ccswitch/gitrepos.py` (`_run_gh(token=)`
+ test); `server/github/pr_population.py` (per-owner token routing in both poll fns + test); `.gitignore`
(ignore the App PEM if a default path is documented).

## 9. Operational note (one-time, post-merge)

Copy App 4031757's private key from `pr-dashboard/config.json` into a `0600` PEM file; set
`CCSWITCH_GITHUB_APP_ID=4031757` and `CCSWITCH_GITHUB_APP_PRIVATE_KEY_PATH=<file>` (leave
`INSTALLATION_ID` unset so the registry maps all owners). Until set, polling stays on the user token.

## 10. Risks

- **Installation coverage** — App 4031757 must be installed on every watched owner (cairnea, steiner385).
  An uncovered owner falls back to the user token (logged once) — correct, but the rate-limit win only
  applies to covered owners. The registry + per-owner routing is exactly why we mimic Plotroom rather
  than use one global token.
- **Private key handling** — `0600`, git-ignored, never logged; tests use an ephemeral keypair, never the
  real key.
- **`gh` GH_TOKEN precedence** — confirmed `gh` prefers `GH_TOKEN`; `_run_gh(token=)` sets it explicitly.
- **No signature-change fan-out** — `_run_gh` gains an optional kwarg; whole-suite run still recommended.
