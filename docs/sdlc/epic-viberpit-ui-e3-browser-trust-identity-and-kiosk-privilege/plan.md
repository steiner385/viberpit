---
initiative: epic-viberpit-ui-e3-browser-trust-identity-and-kiosk-privilege
kind: plan
epic: steiner385/viberpit#1396
upstream: [docs/sdlc/epic-viberpit-ui-e3-browser-trust-identity-and-kiosk-privilege/spec.md]
verified_against: 1a66f955
---

# Plan — ViberPit UI E3: Browser trust, identity and kiosk privilege

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e3-browser-trust-identity-and-kiosk-privilege` |
| **Stage** | Plan (propose-only; merges at a human gate, same as the spec) |
| **Spec** | [`spec.md`](./spec.md) — read-only input, not modified by this plan |
| **Planned against** | `1a66f955` (master, 2026-10-15) |
| **Epic / children** | [#1396](https://github.com/steiner385/viberpit/issues/1396) · G10 [#1408](https://github.com/steiner385/viberpit/issues/1408) · G11 [#1409](https://github.com/steiner385/viberpit/issues/1409) · G12 [#1410](https://github.com/steiner385/viberpit/issues/1410) (all three still OPEN) |

## 0. What changed between the spec (`4aa53194`) and this plan (`1a66f955`)

The spec's findings still hold. Nothing on master reads `Origin`, `grep -rni kiosk server/` finds only
comments (`server/api/ratelimit.py:23`, `server/api/web_vitals.py:54`), `server/auth/webauthn.py` is
still 44 lines of stubs, and `/api/auth/me` still returns a literal `"authMode": "tailscale"`
(`server/api/auth.py:41`). No open PR touches `server/auth/`, `server/assistant/policy.py`,
`server/bridge/ws.py`, `web/src/shell/kiosk.ts` or anything WebAuthn (checked by file via GraphQL
on 2026-10-15). What has drifted, and how this plan handles it:

| Spec says | Master `1a66f955` | Plan consequence |
|---|---|---|
| `CCSWITCH_*` env names throughout (§4) | Settings are aliased `VIBERPIT_*` (`server/config.py:120-131`); new `CCSWITCH_*` names trip the brand ratchet | Every new variable ships as **`VIBERPIT_*`**: `VIBERPIT_ALLOWED_ORIGINS`, `VIBERPIT_EXPOSURE`, `VIBERPIT_WEBAUTHN_ENABLED`, `VIBERPIT_SESSION_IDLE_S`. The env-compat mirror still accepts the legacy prefix; no new `CCSWITCH_*` spelling is introduced. |
| `README.md:182` documents `CCSWITCH_WEBAUTHN_ENABLED` | `README.md:187` now documents `VIBERPIT_WEBAUTHN_ENABLED` and claims passkey auth "is implemented but disabled" | The name mismatch is gone; the claim is still false. T12.5 makes it true and keeps the line. |
| `app.py:184-209` guard | Now `server/app.py:218-224` (`_is_loopback` literal at :218) | Same code, new line numbers. |
| `/ws` at `server/bridge/ws.py:421`, `/ws/vnc` at `vnc_ws.py:147`, `/ws/stt` at `stt/ws.py:57` | `ws.py:568`, `vnc_ws.py:150`, `stt/ws.py:58` | Path-only drift. |
| `require_ws_identity` refuses `read` `ccas2:` at `middleware.py:440-443` | `server/auth/middleware.py:538-570` | Path-only drift. |
| `_signing_secret` at `middleware.py:83-91` | `server/auth/middleware.py:110` | Path-only drift. |
| Newest migration `0035_error_signatures` | Newest is `0087_needs_you_hygiene` | Each child chains off whatever is newest **when it is implemented**, not off a number written here ("duplicate migration number" trap). |
| "the redaction test … already covers `ccas` prefixes" (AC-G10.10) | `server/audit.py`'s shared redaction list carries no `ccas` pattern; no test in `server/tests/test_redaction.py` mentions `ccas` | The premise is false. T10.1 adds the `ccas1:`/`ccas2:` shape to the one shared list in `server/audit.py` and pins it, rather than "extending" a test that does not exist. |
| Display allowlist: `GET /api/pipeline/snapshot`, `/events`, `/api/config`, `/api/auth/me` (FR-G10.5) | The pipeline page also reads `GET /api/pipeline/repos`, `GET /api/pipeline/insights/checks` and `GET /api/prs` | The spec's seed list would leave the kiosk page partially 403. T10.3 derives the allowlist from a test that records what the pipeline page actually fetches, and each extra route gets the FR-G10.7 projection audit. |
| `webauthn` "declared" (`pyproject.toml:27`) | `pyproject.toml:32`, **unpinned** | T12.4 pins it (constitution VII) in its own commit, inside the G12 server PR. |

## 1. Global constraints (every task)

- Branch each PR off a fresh `origin/master`. Before starting each PR, re-run the in-flight-by-file
  GraphQL check for its files (spec §5.8).
- TDD: every task's tests are written first and observed red against master before implementation.
- **No prompts in tests** (#1409): mutation-boundary tests hit `DELETE /api/sessions/{id}/queue`,
  `POST /api/sessions/{id}/fix-pr` with a stubbed handler, or `cancelQueued`/`detach` WS frames, never
  `prompt`.
- Run all four `lint-and-test` steps and the full `frontend` script set before each PR is marked ready
  (AC-X.1, AC-X.2). Server suite: `env -u VIBERPIT_SESSIOND_* -u VIBERPIT_DAEMON_ROLE … pytest
  server/tests/ -n auto`, backgrounded, tee'd to `/tmp/`.
- New columns are nullable and auto-reconciled; one alembic revision per schema-changing PR,
  `down_revision` = newest at implementation time. The G12 server PR adds a **model**, so it also runs
  the Postgres suite with `VIBERPIT_TEST_PG_URL` and adds `_REQUIRED_EXTRAS` /
  `RECONCILE_EXEMPT` entries (AC-X.3).
- Timestamps via `server/db/clock.py:db_utc_now()` only (AC-X.4).
- Replaced code is deleted in the same PR (AC-X.5): listed per task below.
- Each child closes with a live smoke on the tailnet edge, recorded in the PR (AC-X.7), with
  sanitized, seeded-fixture evidence only (AC-X.6, P8).

## 2. Gate decisions this plan depends on

Tasks that read a decision are marked **[gate: Pn]**. Work that does not depend on a decision is
ordered first, so nothing idles waiting on the gate.

| Decision | Blocks | Plan default if ratified as recommended |
|---|---|---|
| P2 — anonymous LAN branch | T12.2 only | (a) delete `allow_unauthenticated` + `OPEN_LAN_IDENTITY` |
| P3 — credential in a URL | T10.2 (one-time vs reusable token), T11.4 (nginx log line) | reusable token + runbook line disabling query-string logging on the vhost |
| P6 — enforce-on-land vs log mode | T11.2 | enforce on land; no log-only mode |
| P7 / Q2 — display expiry, peer pinning, ambient requirement | T10.2 | 30 d default / 365 d max, token-only, no pinning |
| Q3 / P4 — what a wall shows | T10.4 | strip identities, goals, hostnames; keep repo/PR/branch/session names |
| Q6 — ASVS level | T12.7 | L2 for `lan`/`public`, L1 for `tailnet` |
| P5 — passkey recovery | T12.4 | tailnet path only; no CLI recovery command |

## 3. Order

```
PR-A  T11.1  allowed-origins setting (small, lands first; G11 and G12 both consume it)
PR-B  T11.2–T11.5  G11 origin boundary + security headers + ingress contract     [gate: P6, P3]
PR-C  T10.1–T10.4  G10 server: display scope, mint/exchange, allowlist, projection [blocked by PR-B; gate: P3, P7, Q3]
PR-D  T10.5–T10.7  G10 web: /kiosk route, scope-driven interactivity, Fix/socket gating, token list [blocked by PR-C]
PR-E  T12.1–T12.3  G12 exposure policy, one loopback predicate, real authMode      [blocked by PR-A; T12.2 gate: P2]
PR-F  T12.4–T12.5  G12 server: WebAuthn ceremonies, passkey cookie, idle timeout, rate limit [blocked by PR-E]
PR-G  T12.6–T12.7  G12 web: /login, Settings → Security, Playwright virtual authenticator, ASVS record [blocked by PR-F]
```

PR-C/D and PR-E/F/G are independent lanes after PR-B and PR-A respectively and may run in parallel
sessions; the only shared file between the lanes is `server/auth/middleware.py`, so whichever lands
second rebases. `server/api/auth.py` (`/me`) is touched by PR-C (scope, display identity) and PR-E
(`authMode`); PR-E builds `authMode` on whatever PR-C landed and must not re-derive scope.

---

## 4. Tasks

### G11 — origin boundary (#1409)

#### T11.1 — One allowed-origins source (PR-A) · FR-G11.1

**Files**

| File | Purpose |
|---|---|
| `server/config.py` | `allowed_origins: str` (`VIBERPIT_ALLOWED_ORIGINS`) in **both** Settings variants (pydantic at ~:120 and the dataclass fallback at ~:477), plus an `effective_allowed_origins` property: configured ∪ `http://127.0.0.1:<port>`, `http://localhost:<port>` ∪ (when `VIBERPIT_DEV=1`) the Vite dev origins. Entries normalized (lower-case scheme/host, default ports dropped); malformed entries are a startup `ValueError`. |
| `web/vite.config.ts` | Read-only reference: the dev port lives here. The Vite origins are a constant in `server/config.py` with a comment pointing at this file — the one place they are named. |
| `server/app.py` | Log the effective list once in the lifespan. |
| `server/tests/test_config_allowed_origins.py` | New; settings-level tests (no auth logic, so not a parallel auth test file). |

**Tests**

- `test_effective_origins_include_loopback_for_port` — proves AC-G11.5, FR-G11.1
- `test_dev_adds_vite_origins_and_prod_does_not` — proves AC-G11.5
- `test_malformed_origin_refuses_startup` — proves FR-G11.1
- `test_both_settings_variants_expose_allowed_origins` — guards the pydantic/dataclass split

#### T11.2 — One provenance check, two call sites (PR-B) · FR-G11.2, .3, .4, .7 · [gate: P6]

**Files**

| File | Purpose |
|---|---|
| `server/auth/identity.py` | `browser_origin_allowed(conn, *, ambient: bool) -> bool` beside `verify_identity`: the **only** reader of the `Origin` header in `server/`. Implements the FR-G11.3 table and the `Sec-Fetch-Site: cross-site` rejection (FR-G11.4). Origin compared after the same normalization T11.1 applies. |
| `server/auth/middleware.py` | `IdentityMiddleware.dispatch`: after identity is resolved and before `call_next`, non-`GET/HEAD/OPTIONS` requests from a non-bearer caller go through the check → `403 {"code": "ORIGIN_FORBIDDEN"}`. `require_ws_identity`: on the ambient branch only (after the `ccas2:` branch returns), check before returning → `close(1008)`. Requests that are unauthenticated but reach an exempt mutation (the G10 exchange, the G12 login) are treated as **ambient** for origin purposes — the decision table has no "unauthenticated" column, and AC-G10.6 requires the exchange to be origin-checked. |
| `server/audit.py` (consume) | One `auth.origin_rejected` audit event per rejection via `record_audit`: surface (`http`/`ws`/`ws/stt`/`ws/vnc`), peer, origin truncated to 200 chars, no identity. |
| `scripts/smoke_assistant.py` | Send an allowed `Origin` (it is a non-browser ambient caller). |

**Tests** (extend existing files; no parallel WS-auth file)

- `test_ws_external_token.py::test_ambient_ws_origin_matrix[ws|ws/stt|ws/vnc × allowed|disallowed|null|absent]` — uses `_closed_before_accept`, asserts code 1008 — proves AC-G11.1
- `test_ws_external_token.py::test_ccas2_act_token_without_origin_opens_ws` — proves AC-G11.2
- `test_auth.py::test_ccas1_loopback_post_without_origin_allowed` — proves AC-G11.2
- `test_auth.py::test_ambient_mutation_origin_matrix` — spy on a stubbed `DELETE /api/sessions/{id}/queue` handler; asserts the spy is **not** called on 403 — proves AC-G11.3
- `test_auth.py::test_ambient_get_with_foreign_origin_is_200` — proves AC-G11.3
- `test_auth.py::test_sec_fetch_site_cross_site_rejected_despite_allowed_origin` — proves AC-G11.4
- `test_auth_failure_audit.py::test_origin_rejection_audits_surface_peer_bounded_origin_no_identity` — proves AC-G11.7
- `test_stt_ws.py::test_stt_ws_rejects_foreign_origin` — proves AC-G11.1 at the STT route specifically
- `test_auth.py::test_origin_header_read_in_exactly_one_place` — AST/grep over `server/` for `headers.get("origin")` / `headers["origin"]`, expected count 1; mutate a copy to prove it can fail — proves AC-G11.8
- Live: `scripts/smoke_assistant.py` against a locally started edge with enforcement on — proves AC-G11.8

**Sibling sweep (capturing-fix-patterns).** Every web call site that issues a mutation goes through
`web/src/api/client.ts`; confirm no `fetch(` with a non-GET method bypasses it, and that
`navigator.sendBeacon` users (web-vitals) hit a route that is either GET-exempt or same-origin-sent.
`sendBeacon` sends `Origin`, so it passes; the test pins that.

#### T11.3 — Security response headers (PR-B) · FR-G11.5

**Files**

| File | Purpose |
|---|---|
| `server/app.py` | One pure-ASGI headers middleware (not `BaseHTTPMiddleware`, so it also wraps static and error responses) adding `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`. Registered outermost so 401/403 from `IdentityMiddleware` carry them. Grep first for an existing response-header middleware (e.g. `server/obs/middleware.py`'s `X-Request-ID`) and extend it if it is the right altitude rather than adding a second. |

**Tests**

- `test_auth.py::test_security_headers_on_healthz_401_and_static` — parametrized over `/healthz`, an unauthenticated `/api/sessions` (401), and a static asset — proves AC-G11.6
- `test_auth.py::test_hsts_absent_from_app_responses` — proves AC-G11.6

Note the `web/dist` trap: a built `web/dist` mounts the SPA catch-all and breaks two traversal tests;
the static-asset case uses a temp static dir fixture, not the real `web/dist`.

#### T11.4 — Ingress contract (PR-B) · FR-G11.6 · [gate: P3]

| File | Purpose |
|---|---|
| `deploy/viberpit-tailnet.nginx`, `deploy/ccswitch-tailnet.nginx` | `add_header Strict-Transport-Security "max-age=31536000" always;`; and, if P3 is ratified as recommended, an `access_log` format without `$args` for the `/kiosk` location. |
| `deploy/README.md` | One section: origin enforcement lives in the app; nginx must forward `Origin` unchanged; HSTS lives here; applying the edit is an nginx reload that does not touch the daemon (P10). |

**Tests:** `test_deploy_nginx_contract.py::test_vhosts_carry_hsts_and_do_not_rewrite_origin` (file
assertions; new file because no nginx-contract test exists — check `server/tests/` for one first) —
proves AC-G11.6, FR-G11.6.

#### T11.5 — G11 live smoke (PR-B, post-merge) · AC-X.7

After deploy: from a page served on a different tailnet origin, `new WebSocket('wss://<edge>/ws')`
closes 1008 and a cross-origin `fetch(..., {method: 'DELETE'})` is 403; the operator's own console
still works. Recorded in the PR as a comment with sanitized output.

### G10 — display principal (#1408), blocked by G11

#### T10.1 — Scope on the request, the session and the token (PR-C) · FR-G10.1, FR-G10.2, AC-G10.10

| File | Purpose |
|---|---|
| `server/db/models.py` | `AppSession.scope` (nullable, NULL ⇒ `"full"`), `AppSession.token_id` (nullable); `AssistantToken.label` (nullable). `AssistantToken.scope` gains the value `"display"` — comment at :379 updated. |
| `server/db/migrations/versions/00NN_display_scope.py` | Revision for the three columns. |
| `server/auth/middleware.py` | `request.state.scope` / `websocket.state.scope` set by the same code that sets `identity` (`_resolve_or_create_app_session` returns the row's scope). Narrower wins: a display-scoped cookie forces `scope="display"` regardless of ambient identity (FR-G10.4). A display cookie is accepted **without** an ambient identity, re-validating its `token_id` row on every request exactly like `_validate_user_token` (revoked/expired → revoke the `AppSession`, 401). |
| `server/assistant/token.py` | Display tokens reuse `USER_PREFIX` (`ccas2:`) and its serializer with `scope="display"` in the payload — **no new prefix**, so no new brand-ratchet entry and one validator. `_validate_user_token` refuses `scope="display"` as a bearer (display tokens are exchange-only). |
| `server/audit.py` | Add the `ccas[12]:` token shape to the shared redaction list (see §0: the premise that it is already there is false). |

**Tests**

- `test_auth_session_reuse.py::test_display_cookie_overrides_full_ambient_identity` — proves AC-G10.4, FR-G10.4
- `test_auth.py::test_display_cookie_without_ambient_identity_is_admitted` (peer `203.0.113.7`, `whois` → None) — proves AC-G10.2 (admission half)
- `test_auth.py::test_revoked_or_expired_display_token_kills_cookie_session` — proves AC-G10.5
- `test_ws_external_token.py::test_display_token_rejected_as_bearer` — exchange-only
- `test_redaction.py::test_ccas_tokens_redacted[ccas1|ccas2-display|ccas2-act]` — proves AC-G10.10
- `test_schema_reconcile_coverage.py` passes unchanged (nullable columns) — AC-X.3

#### T10.2 — Mint and exchange (PR-C) · FR-G10.2, FR-G10.3, FR-G10.9 · [gate: P3, P7/Q2]

| File | Purpose |
|---|---|
| `server/assistant/api.py` | The existing `POST /api/assistant/tokens` accepts `scope: "display"`, `label`, `expiresInDays` (default 30, max 365); minting a display token requires full scope. List/revoke routes include display tokens and accept a `?scope=` filter. **No `/api/kiosk/tokens` alias** — one URL, one implementation (spec FR-G10.9 leaves the choice to Plan). |
| `server/api/kiosk.py` | New, single route: `POST /api/kiosk/session` accepting a **form** body `token=` (so the SPA can submit a native form: the browser sends `Origin`, follows the 303, stores the cookie, and the token never re-enters a URL). Validates the token, mints an `AppSession(scope="display", token_id=…, expires_at=min(now+24h…, token.expires_at))`, 303 to the first kiosk page. Revoked/expired → 401, no cookie, one audit event. If P3 picks one-time tokens, the exchange also stamps `consumed_at` and refuses a second use. |
| `server/auth/middleware.py` | Exempt `POST /api/kiosk/session` from the identity requirement (it authenticates by body) but **not** from the T11.2 origin check. |
| `server/app.py` | Mount the kiosk router. |

**Tests** (new `server/tests/test_kiosk_display_scope.py` — the one file for the display principal)

- `test_full_identity_mints_display_token_with_label_and_expiry` — proves FR-G10.2
- `test_display_identity_cannot_mint` — proves FR-G10.5 (token routes forbidden)
- `test_exchange_sets_display_cookie_and_strips_token_from_location` — row has `scope="display"`, `token_id`, `expires_at ≤ token.expires_at`; `Location` has no `token=` — proves AC-G10.1
- `test_exchange_revoked_or_expired_token_401_no_cookie_one_audit` — proves AC-G10.5
- `test_exchange_from_disallowed_origin_403` — proves AC-G10.6
- `test_mint_exchange_revoke_audit_never_contains_token` — proves AC-G10.10

#### T10.3 — Display route allowlist and socket refusal (PR-C) · FR-G10.5, FR-G10.6

| File | Purpose |
|---|---|
| `server/assistant/policy.py` | `DISPLAY_ROUTE_ALLOW` beside `ASSISTANT_ROUTE_ALLOW`/`OPERATOR_ROUTE_ALLOW`; `route_allowed(method, path, *, operator=False, display=False)` consults only it when `display=True`. Seed: `GET /api/pipeline/snapshot`, `GET /api/pipeline/events`, `GET /api/config`, `GET /api/auth/me`, `POST /api/auth/logout`, plus whichever of `GET /api/pipeline/repos`, `GET /api/pipeline/insights/checks`, `GET /api/prs` the kiosk page is shown to need (below). |
| `server/auth/middleware.py` | Display-scoped requests go through `route_allowed(..., display=True)` → `403 {"code": "DISPLAY_SCOPE_FORBIDDEN"}`. `require_ws_identity` closes 1008 for a display-scoped cookie on all three sockets (same rationale as the `read`-scope refusal at :552). |

**Deriving the allowlist.** First, a web test renders `PipelinePage` in kiosk with a recording fetch
mock and asserts the exact set of URLs requested. That set — not the spec's seed list — is the
allowlist, and the test is the guard that `showInKiosk` and `DISPLAY_ROUTE_ALLOW` stay reviewed
together (FR-G10.5). Any read it adds beyond the spec's four goes through T10.4's projection audit.

**Tests**

- `test_kiosk_display_scope.py::test_display_scope_route_table` — enumerates `app.routes`; every
  (method, path) outside the allowlist and static → 403 `DISPLAY_SCOPE_FORBIDDEN`; every allowlisted
  route → 200 (first SSE frame for `/events`). Table-driven over the real router — proves AC-G10.2
- `test_kiosk_display_scope.py::test_display_cookie_refused_on_all_sockets[ws|ws/stt|ws/vnc]` — proves AC-G10.3, FR-G10.6
- `web/src/features/pipeline/PipelinePage.test.tsx::kiosk page requests only the display allowlist` — proves FR-G10.5 (pairing)
- Coverage on `server/assistant/policy.py` ≥85% — constitution V

#### T10.4 — Minimized detail and `/me` shape (PR-C) · FR-G10.7, FR-G10.1 · [gate: Q3/P4]

| File | Purpose |
|---|---|
| `server/api/pipeline.py` | One `project_for_display(payload)` applied at the endpoint when `request.state.scope == "display"` for `snapshot`, `events` and any extra route T10.3 admitted. Strips `@`-bearing identities, session goals/prompts, `.ts.net` hostnames. Applied in the endpoint, not the client. |
| `server/api/auth.py` | `/me` returns `{identity, scope, authMode}`; display returns `{"identity": "display:<label>", "scope": "display", "authMode": "display"}`. `authMode` for other callers stays `"tailscale"` here; PR-E makes it real. |

**Tests**

- `test_kiosk_display_scope.py::test_display_payloads_have_no_identity_goal_or_hostname` — seeded fixtures containing all three in full scope; scan serialized bodies — proves AC-G10.7
- `test_kiosk_display_scope.py::test_me_for_display_hides_minting_operator` — proves AC-G10.4, FR-G10.7
- `test_auth.py::test_me_reports_full_scope_for_ambient` — proves FR-G10.1

#### T10.5 — Client follows the server (PR-D) · FR-G10.8

| File | Purpose |
|---|---|
| `web/src/api/` (auth hook) | One `useAuthMe()` hook returning `{identity, scope, authMode}`. Grep first: `web/src/api/quickReplies.ts:8` already queries `/auth/me` for `identity` — move that query into the new hook and have `quickReplies` consume it (one query key, not two). Add to the shared `@/api/hooks` mocks the ~47 test files use. |
| `web/src/shell/kiosk.ts`, `web/src/shell/AppShell.tsx` | `mode === 'kiosk'` when `?kiosk=1` **or** `scope === "display"`. `readKioskConfig` stays a layout parser; it does not learn about tokens. |
| `web/src/shell/InteractionContext.tsx` | `interactive = false` derived from the same mode. |
| `web/src/shell/useAppBanners.tsx`, `web/src/hooks/useInputHold.ts` | Do not open the session socket when not interactive (conditional subscription in `useInputHold`, since hooks cannot be called conditionally). |
| `web/src/features/pipeline/PipelinePage.tsx`, `web/src/pipeline/PrRow.tsx` | Pass `onFixPr` only when interactive; `PrRow` additionally checks `useInteractive()` so the Fix button cannot render in kiosk regardless of the caller. |
| `web/src/shell/navDefs.ts` | Update the "verified read-only" comment: the verification is now `DISPLAY_ROUTE_ALLOW`. |
| Display-mode banner | "Display mode — sign out to resume", reusing the existing banner stack (tone `info`, admitted in kiosk only when scope is display). |

**Tests**

- `PipelinePage.test.tsx::display scope without ?kiosk=1 renders kiosk, no Fix, no WebSocket` (spy on `WebSocket`) — proves AC-G10.8
- `PipelinePage.test.tsx::?kiosk=1 with full scope renders kiosk, no Fix, no WebSocket` — proves AC-G10.8
- `PipelinePage.test.tsx::neither renders Fix` — proves AC-G10.8
- `PrRow.test.tsx::Fix is not rendered when not interactive even with onFixPr` — proves FR-G10.8
- `useAppBanners` test: no socket opened when not interactive — proves FR-G10.6 (client half)

#### T10.6 — `/kiosk` route (PR-D) · FR-G10.3, Q8

| File | Purpose |
|---|---|
| `web/src/shell/routes.tsx` + new `web/src/features/kiosk/KioskExchange.tsx` | `/kiosk?token=…`: immediately `history.replaceState` to drop the token from the visible URL, then submit a native `<form method="post" action="/api/kiosk/session">` with the token. Error state for 401/403 ("This display link has expired or been revoked"). |

**Tests:** `KioskExchange.test.tsx::strips token from URL before submitting`, `::renders expired message on 401` — prove FR-G10.3. e2e `web/e2e/kiosk-display.spec.ts`: mint (seeded) → open `/kiosk?token=` → lands on `/pipeline`, rail absent, a forced `POST` is 403 — proves AC-G10.1/2 end to end (the e2e job is not required in CI, so run it locally before marking ready).

#### T10.7 — Token management UI (PR-D) · FR-G10.9

| File | Purpose |
|---|---|
| `web/src/features/settings/` (new `DisplayTokensSection.tsx` on the existing settings chassis) | List label / created / expires / last used, mint (show once with a copyable `/kiosk?token=` link), Revoke via the existing revoke route. Hidden for display scope. |

**Tests:** `DisplayTokensSection.test.tsx::lists tokens with label, expiry, last used`, `::revoke calls the existing revoke route`, `::absent for display scope` — prove AC-G10.9.

**G10 live smoke (AC-X.7):** a display link on a non-operator device renders the pipeline, a forced
mutation from devtools is 403, `/ws` is never attempted, and revoking from Settings ends the display
on its next poll.

### G12 — exposure policy and passkeys (#1410)

#### T12.1 — One exposure validator, one loopback predicate (PR-E) · FR-G12.1, .2, .4

| File | Purpose |
|---|---|
| `server/config.py` | `exposure: Literal["loopback","tailnet","lan","public"]` (`VIBERPIT_EXPOSURE`, default `tailnet`), `webauthn_enabled` (`VIBERPIT_WEBAUTHN_ENABLED`), `session_idle_s` (`VIBERPIT_SESSION_IDLE_S`, 28800) — both Settings variants. |
| `server/auth/exposure.py` | New, single: `validate_exposure(settings) -> None` implementing the FR-G12.2 table; each violation raises `RuntimeError("exposure=<tier>: <rule> — fix: <fix>")`. `VIBERPIT_DEV=1` downgrades only `loopback`/`tailnet` violations to one warning. `lan`/`public` with no `VIBERPIT_SESSION_SECRET`/master key refuses (closes `_signing_secret`'s ephemeral fallback there). The `lan` "≥1 registered credential" rule is a DB read, so it runs in the lifespan, not at import. |
| `server/app.py` | Delete the guard at :218-224 (including the `_is_loopback` literal) and call `validate_exposure`. |
| sweep | Every `"127.0.0.1"` / `"::1"` / `"localhost"` comparison in `server/` routes through `identity.host_is_loopback()`; master has one (`app.py:218`) — the grep is re-run at implementation time and recorded in the PR. |
| ~15 test files setting `VIBERPIT_DEV=1` | Audit each: it either sets `VIBERPIT_EXPOSURE=loopback` or keeps DEV for an unrelated reason. Recorded in the PR. |

**Tests** (new `server/tests/test_exposure_policy.py`)

- `test_exposure_matrix[tier × rule]` — every Refuses cell raises with tier + rule in the message; every satisfied row starts — proves AC-G12.1
- `test_dev_relaxes_only_loopback_and_tailnet` — proves AC-G12.1
- `test_lan_and_public_require_persistent_secret`, `test_tailnet_keeps_one_shot_warning` — proves AC-G12.9
- `test_no_loopback_literal_comparisons_outside_identity` (grep/AST guard, proven able to fail) — proves AC-G12.2

#### T12.2 — Reconcile `lan@local` (PR-E) · FR-G12.3 · [gate: P2]

- **(a) removal, recommended:** delete `allow_unauthenticated` (both Settings variants), `OPEN_LAN_IDENTITY` and the branch at `identity.py:~132`; rewrite (not delete) `test_auth.py`'s LAN tests and `test_auth_identity_peer_trust.py`'s two references to pin "unidentified → 401 in every tier". Proves AC-G12.3 (removal).
- **(b) display-only:** blocked until an owner amendment PR to `.specify/memory/constitution.md` has merged (agents must not write it). Then the anonymous branch yields `scope="display"`; AC-G10.2/3 tests are parametrized over it. Proves AC-G12.3 (display-only).
- **(c):** not planned; would also need the amendment.

#### T12.3 — Real `authMode` (PR-E) · FR-G12.5

| File | Purpose |
|---|---|
| `server/auth/middleware.py` | Set `request.state.auth_method` ∈ `tailscale | passkey | display | bearer` where identity is resolved (same place as scope). |
| `server/db/models.py` + revision | `AppSession.auth_method` (nullable, NULL ⇒ `tailscale`), `AppSession.last_seen_at` (nullable). |
| `server/api/auth.py` | `/me.authMode` reads `request.state.auth_method`; the literal at :41 is deleted. |

**Tests:** `test_auth.py::test_me_auth_mode[tailscale|display|bearer]` (passkey added in T12.4) — proves FR-G12.5.

#### T12.4 — WebAuthn server (PR-F) · FR-G12.6–.11 · [gate: P5]

| File | Purpose |
|---|---|
| `pyproject.toml` | Pin `webauthn` to a vetted range (VII), own commit. |
| `server/db/models.py` + revision | `WebAuthnCredential` model per spec §4 (public key only). New **model** → `_REQUIRED_EXTRAS` in `test_postgres_compat.py`, `RECONCILE_EXEMPT` if any NOT NULL column lacks a synthesizable default, Postgres suite run. |
| `server/auth/webauthn.py` | Rewritten over `py_webauthn`: RP id = host of the primary configured origin, expected origins = `settings.effective_allowed_origins` (T11.1 — no second list). Challenges stored server-side, single-use, short TTL. The bare `WEBAUTHN_ENABLED` read and all `NotImplementedError` stubs are deleted. |
| `server/api/auth_webauthn.py` (or added to `server/api/auth.py` if it stays small) | The six routes of FR-G12.7, mounted only when `webauthn_enabled`. First-credential registration requires full scope **and** `auth_method == "tailscale"` for the same identity (FR-G12.8); the identity must be on `tailnet_allowlist`. |
| `server/auth/middleware.py` | Passkey `AppSession` cookie is an authenticator on its own: absolute 24 h, idle `session_idle_s` (touch `last_seen_at`), `Secure` required outside `loopback`. Login begin/complete exempt from identity, not from the origin check. |
| `server/api/ratelimit.py` (consume) | Login rate limiting per peer and per claimed identity through the existing limiter; 429 + `Retry-After`; audit each failure. |

**Tests** (new `server/tests/test_webauthn.py`, software authenticator from `py_webauthn` test vectors or `soft-webauthn` as a dev dep)

- `test_register_then_login_round_trip_sets_secure_httponly_strict_cookie` then cookie-only `/me` → `authMode: "passkey"` — proves AC-G12.4
- `test_register_begin_forbidden_for_non_tailnet_or_display` — proves AC-G12.5, FR-G12.8
- `test_replayed_challenge_400`, `test_revoked_credential_401`, `test_foreign_origin_assertion_401`, `test_sign_count_regression_401_and_audit`, `test_tampered_cookie_401` — prove AC-G12.5
- `test_login_rate_limit_429_retry_after_uses_shared_limiter` (asserts the limiter object `is` `server.api.ratelimit`'s) — proves AC-G12.6
- `test_idle_timeout_expires_and_touch_advances_last_seen`, `test_absolute_24h_still_enforced` — prove AC-G12.7
- `test_delete_credential_revokes_its_sessions_and_tailnet_still_works` — proves AC-G12.8
- `test_routes_404_when_disabled_and_no_not_implemented_in_auth` — proves AC-G12.10
- Coverage on `server/auth/` ≥85% — constitution V

#### T12.5 — Docs in place (PR-F)

`docs/product/features/tailnet-identity-auth.md` updated in place (tiers, passkey path, display scope);
`deploy/README.md` tier runbook; `README.md:187` stays and becomes true. No second doc.

#### T12.6 — WebAuthn client (PR-G) · FR-G12.12

| File | Purpose |
|---|---|
| `web/src/features/auth/LoginPage.tsx` + route | `/login` drives `navigator.credentials.get` via begin/complete; rendered when `/me` is 401 and the tier allows passkeys. `web/src/api/client.ts` redirects 401 to `/login` in those tiers only. |
| `web/src/features/settings/SecuritySection.tsx` | List / enrol / revoke credentials. |

**Tests:** vitest for both components; `web/e2e/passkey-login.spec.ts` using Playwright
`CDPSession` `WebAuthn.addVirtualAuthenticator` — proves AC-G12.11.

#### T12.7 — ASVS record and G12 smoke (PR-G) · FR-G12.13 · [gate: Q6]

The PR body carries the ASVS 4.0 mapping (V2.2, V2.8, V3.2–3.5, V13.2, V14.4) with level per tier and
an explicit list of uncovered controls. Live smoke (AC-X.7): passkey login round-trips on the edge
with a test credential that is revoked afterwards.

---

## 5. Requirement → task traceability

| Requirement | Task | Acceptance |
|---|---|---|
| FR-G11.1 | T11.1 | AC-G11.5 |
| FR-G11.2–.4 | T11.2 | AC-G11.1–.4, .8 |
| FR-G11.5 | T11.3 | AC-G11.6 |
| FR-G11.6 | T11.4 | AC-G11.6 |
| FR-G11.7 | T11.2 | AC-G11.7 |
| FR-G10.1–.2 | T10.1, T10.4 | AC-G10.4, .5, .10 |
| FR-G10.3 | T10.2, T10.6 | AC-G10.1, .5, .6 |
| FR-G10.4 | T10.1 | AC-G10.4 |
| FR-G10.5–.6 | T10.3, T10.5 | AC-G10.2, .3 |
| FR-G10.7 | T10.4 | AC-G10.7 |
| FR-G10.8 | T10.5 | AC-G10.8 |
| FR-G10.9 | T10.2, T10.7 | AC-G10.9 |
| FR-G12.1–.2, .4 | T12.1 | AC-G12.1, .2, .9 |
| FR-G12.3 | T12.2 | AC-G12.3 |
| FR-G12.5 | T12.3 | (no AC in spec; tests in T12.3) |
| FR-G12.6–.11 | T12.4 | AC-G12.4–.8, .10 |
| FR-G12.12 | T12.6 | AC-G12.11 |
| FR-G12.13 | T12.7 | — |

## 6. Risks

| Risk | Mitigation |
|---|---|
| **Self-inflicted console outage** (P6): a wrong `VIBERPIT_ALLOWED_ORIGINS` 403s every mutation and closes every socket on the operator's only console. | Loopback origins are always in the effective list, so `ssh dobby` + `127.0.0.1:8787` still works; the effective list is logged at startup; PR-B's body states the exact prod value to set **before** the deploy, and the AC-X.7 smoke runs immediately after. |
| **Non-browser ambient callers break** (smoke script, ad-hoc `curl` POSTs from operator shells, any internal loopback caller without a bearer). | T11.2 sweeps `server/` and `scripts/` for loopback HTTP clients that mutate; each gets a bearer or an `Origin`. The assistant tool path already uses `ccas1:` (exempt). |
| **Vite dev proxy breaks dev** because `changeOrigin` rewrites `Host`, not `Origin`. | Dev origins added under `VIBERPIT_DEV=1` (T11.1); AC-G11.5 pins both directions. |
| **Display allowlist leaves the kiosk page half-empty** (spec seed list omits `/api/pipeline/repos`, `/insights/checks`, `/api/prs`). | T10.3 derives the list from what the page fetches, under a test. |
| **Display projection leaks via a field added later** to `snapshot`. | The AC-G10.7 scan is over the serialized body against fixtures holding all three sensitive kinds, so a new leaking field fails it without anyone updating the test. |
| **Token in nginx access logs** (P3). | `Referrer-Policy: same-origin`, token stripped client-side before submit, form POST body (not query) for the exchange, vhost log line per gate decision. |
| **Two lanes edit `server/auth/middleware.py`** (PR-C and PR-E/F). | Whichever lands second rebases; scope and auth_method are set at one site, and PR-E reads PR-C's scope rather than re-deriving it. Re-check merged state before resolving conflicts. |
| **Migration number collision** between the G10 and G12 lanes. | Pick the number at implementation time; before marking ready, `alembic heads` must show one head. |
| **Postgres-only red** from the new `webauthn_credentials` model. | Run the Postgres suite locally with `VIBERPIT_TEST_PG_URL`; add `_REQUIRED_EXTRAS`/`RECONCILE_EXEMPT` entries in the same PR. |
| **Web mock drift**: a new `useAuthMe` used in the shell breaks the ~47 test files that hand-mock `@/api/hooks`. | Add it to the shared mocks; run the **full** `npm run test`. |
| **Removing `lan@local` strands a real LAN user** (P2a). | Only the operator uses it today; PR-E's runbook entry names passkey enrolment over the tailnet as the replacement. If the gate wants no window without a LAN path, T12.2 moves from PR-E into PR-F so removal and passkeys land together. |
| **Passkey lockout** (P5) on a `public` deployment with tailnet also lost. | Accepted per gate; documented in the runbook. `public` is not turned on by this epic. |
| **Daemon/edge restart while sessions run.** | All changes ship through the deploy controller; the nginx edit is a reload, not a daemon restart (P10). |
| **Live smoke touches real data** (P8). | Smokes use a seeded display token and a test passkey revoked afterwards; evidence is sanitized before it is posted. |
