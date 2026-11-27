# UI conformance scorecard

The in-repo "root scorecard/conformance record" that the UI-audit child issues (#1411–#1415,
epic #1398) ask each leaf to update. It replaces the audit's local report
(`~/Documents/ViberPit-UI-Review-2026-09-07/report.html`), which automation cannot read, as the
thing those acceptance lines point at (E5 plan §WS-6, spec §7 P5).

One table per leaf, updated by the PR that moves it. Evidence is sanitized: the cross-browser
specs run on a synthetic, frozen-clock backend (`web/e2e/fixtures/seed.ts`), so no screenshot,
trace or axe report carries a real account, prompt or transcript.

Last updated: 2026-11-27 (G12, #1410).

## G10 — Server-enforced read-only kiosk identity (#1408)

Server half shipped in #3536 (display scope, mint/exchange, `DISPLAY_ROUTE_ALLOW`, socket
refusal, payload minimization); web half in the PR that adds this table.

| Criterion | Status | Evidence |
|---|---|---|
| Mutations and WS control frames denied for kiosk credentials regardless of URL | Done | `server/tests/test_kiosk_display_scope.py`: route-table sweep over the real router (every non-allowlisted route → 403 `DISPLAY_SCOPE_FORBIDDEN`), `/ws`, `/ws/stt`, `/ws/vnc` → 1008. |
| Allowed feeds, expiry and revocation verified | Done | Same file: allowlisted reads 200; revoked or expired token → next request 401 and the session row revoked. |
| Client follows the server (FR-G10.8) | Done | `web/src/shell/kioskDisplayScope.test.tsx`: `scope: "display"` without `?kiosk=1` and `?kiosk=1` with full scope both render kiosk with no Fix and **no `WebSocket` constructed**; the "neither" control renders Fix and opens `/ws`. |
| Page reads paired with the allowlist (FR-G10.5) | Done | `PipelinePage.test.tsx` "kiosk page requests only the display allowlist" reads `DISPLAY_ROUTE_ALLOW` from `server/assistant/policy.py` itself — no copy. |
| Revocable, least-privilege identity a person can manage | Done | Settings › Display links (`DisplayLinksSection`): mint (link shown once), list label/created/expires/last used, Revoke via the existing route; absent for a display session. `/kiosk?token=` strips the token from the URL before posting it as a form body. |
| Sibling audit, existing source of truth | Done | One `/auth/me` query (`useAuthMe`, now also behind quick replies); one `/ws` switch (`setSessionSocketsAllowed`) instead of a check in each of the six socket-reaching hooks; the existing token list/revoke routes, not an alias. |

### Before / after (deliberate breakage)

Each guard was broken on purpose and the test run, then restored.

| Seeded gap | Before | After |
|---|---|---|
| A display whose URL lost `?kiosk=1` opened `/ws` before its scope was known (hold removed: `socketsAllowed = !kiosk.enabled`) | red: `expected [ '/ws' ] to deeply equal []` | green |
| A kiosk page read outside the allowlist (`/api/pipeline/snapshot` removed from `DISPLAY_ROUTE_ALLOW`) | red: `/api/pipeline/snapshot is not in DISPLAY_ROUTE_ALLOW` | green |
| Before #1408 at all: `?kiosk=1` was the only kiosk, the credential behind it the operator's full identity, and a fixable PR's **Fix** rendered on a wall whenever the caller passed `onFixPr` | — | `PrRow` refuses Fix when not interactive; the server 403s the mutation regardless |

### Not done here (and why)

- **Live smoke (AC-X.7)** — a display link on a non-operator device, a forced mutation from
  devtools, Revoke ending the display on its next poll — needs a deployed build; recorded on the
  issue after deploy.
- **Live smoke** also covers the two follow-ups below once deployed: a display link opened from a
  device the tailnet does not identify (#3543), and a wall's network log showing no off-allowlist
  polls (#3544).

### Follow-ups, closed

- **A device with no ambient identity can open `/kiosk`** (#3543). `GET /kiosk` is a small
  server-rendered exchange page (`server/api/kiosk.py`, CSP-hashed inline script, `no-store`,
  `no-referrer`) — not the SPA shell — and is the only path the auth middleware opens without an
  identity. After the exchange the display cookie admits the shell like any display request. The
  static bundle stays behind identity (no exposure-tier change for #1410).
- **A display shell polls only allowlisted routes** (#3544). `api.get` consults one gate
  (`web/src/api/displayGate.ts`) fed by the server's own list (`allowedReads` in `GET /api/auth/me`
  for a display session), so a blocked read never leaves the browser and a new hook is covered
  without an `enabled:` copy. Residual: the first paint races `/auth/me`, so each off-list query
  can still 403 once before the scope is known.

## G12 — Exposure policy and strong authentication (#1410)

Server, web and docs in one PR (plan PR-E/F/G folded together). Gate decisions taken at the plan's
recommended defaults, as G10 did: P2 (a) remove the anonymous LAN branch, P5 tailnet-only recovery,
Q6 ASVS L2 for `lan`/`public` and L1 for `tailnet`.

| Criterion | Status | Evidence |
|---|---|---|
| Fail-closed exposure policy (FR-G12.1/.2, AC-G12.1) | Done | `server/tests/test_exposure_policy.py`: every tier's satisfied config starts; 20 refusal cells each raise naming tier + rule + fix; `VIBERPIT_DEV=1` relaxes only loopback/tailnet (one warning) and never lan/public. The lifespan runs it (`test_security_hardening.py` Part E). |
| One loopback predicate (FR-G12.4, AC-G12.2) | Done | AST guard `test_no_loopback_literal_comparisons_outside_identity`, proven able to fail on the old `app.py` shape. The one other hit, `library/plugin_policy.py`'s SSRF refusal of the hostname `localhost`, is a DNS-name rule `host_is_loopback` (IP-only) cannot answer, so it stays. |
| No unidentified caller in any tier (FR-G12.3, AC-G12.3) | Done | `test_auth.py::test_unidentified_caller_is_401_in_every_tier` (with the deleted flag still in env); `allow_unauthenticated` and `OPEN_LAN_IDENTITY` gone; tests that used LAN-open to stand for an agent-class caller now assert as `lan@local` explicitly. |
| Real `authMode` (FR-G12.5) | Done | `/api/auth/me` reads `request.state.auth_method` (`tailscale`/`passkey`/`display`/`bearer`), set where identity is resolved. |
| Passkey ceremonies with a software authenticator (AC-G12.4) | Done | `server/tests/test_webauthn.py` with `_soft_authenticator.py` (ES256, `none` attestation) driving real py_webauthn verification: register as a tailnet identity → login with no ambient identity → `Secure; HttpOnly; SameSite=Strict` cookie → cookie-only `/me` = `passkey`. |
| Negative identity paths (AC-G12.5) | Done | Same file: enrol refused for unidentified (401), display scope, and a non-person local caller (403); replayed challenge 400; revoked credential 401; foreign-origin assertion 401 + audit; sign-count regression 401 + audit; tampered cookie 401. |
| Login rate limit (AC-G12.6) | Done | 429 + `Retry-After` after the burst; the limiter is `server/api/ratelimit.py`'s (asserted by identity); one `auth.passkey.fail` audit row per failure. |
| Idle/absolute expiry (AC-G12.7) | Done | Touch advances `last_seen_at`; idle + 1 s → 401; past the 24 h absolute → 401. |
| Revocation and tailnet recovery (AC-G12.8) | Done | Delete revokes the credential and its sessions; the same identity still signs in over Tailscale. |
| Off means absent (AC-G12.10) | Done | Flag off → all six routes 404, `/api/config.webauthnEnabled=false`, passkey sessions stop authenticating; `grep NotImplementedError server/auth/` empty. |
| Persistent secret in lan/public (AC-G12.9) | Done | `test_lan_and_public_require_persistent_secret`; tailnet keeps the one-shot warning. |
| Shell reachable for passkey sign-in (FR-G12.12) | Done | With passkeys on, a GET/HEAD the SPA mount answers loads without identity (exact route match: `/docs`, `/openapi.json`, every `/api` route stay 401). Without this a LAN device could not reach `/login`. |
| Web: `/login`, 401 → `/login`, Settings › Security (AC-G12.11) | Done | See the PR's web test list; e2e `web/e2e/passkey-login.spec.ts` uses a CDP virtual authenticator. |

### Not done here (and why)

- **Real-device passkey verification and the live smoke (AC-X.7).** Registration, login, recovery
  and revocation with a real platform authenticator on the deployed edge need a person and a
  deployed build. The issue stays open for that.
- **`public` is not turned on anywhere.** HSTS is attested by the operator
  (`VIBERPIT_INGRESS_HSTS=true`), not probed. No ingress evidence exists, because no public ingress
  exists.

## G13 — Field performance and bundle budget (#1411)

Closed. Shipped in #2319: Core Web Vitals collection, `GET /api/web-vitals/summary`, and the
CI-enforced `web/bundle-budget.json` (`npm run budget`). Not re-measured by this change.

## G14 — Cross-browser visual and accessibility regressions (#1412)

| Criterion | Status | Evidence |
|---|---|---|
| Populated, deterministic fixtures | Done | `web/e2e/fixtures/seed.ts`: seeded sessions/accounts/settings/plugins, frozen clock (`page.clock.setFixedTime`), reduced motion, silent WebSocket. No backend. |
| Chromium + Firefox + WebKit, mode/orientation coverage | Done in CI config; WebKit unverified locally (see below) | `web/playwright.config.ts` (3 projects); `shell-modes.spec.ts`: 4 modes (desktop 1440×900, tablet-landscape 1024×768, phone-portrait 390×844, phone-landscape 844×390) × 5 routes, plus the phone hand-off and desktop non-overlap flows. |
| Geometry and visual assertions | Done (geometry); pixel-diff deliberately not added | `web/e2e/support/geometry.ts`: overflow, min target (24px AA / 44px touch), in-viewport, non-overlap, silent text clipping. Pixel baselines are not committed: they would have to be generated on the CI runner image (fonts and rasterization differ per host and engine), and a baseline made elsewhere fails in CI for reasons that are not regressions. A per-cell screenshot is published instead. |
| Validated accessibility tooling | Done | `@axe-core/playwright` 4.14.1 in the engine under test. Validated, not assumed: `seeded-defects.spec.ts` shows it catches an unnamed button, an image with no alt, an unlabelled input, and low contrast written as `oklch()`, `color-mix()` and hex. |
| Analyzer limitations classified, no blanket suppression | Done | `web/e2e/support/a11y.ts` `ANALYZER_LIMITATIONS` (5 narrow entries) and `docs/product/ui-a11y-analyzer-limitations.md`. An unclassified violation **or** an unclassified "needs review" result fails the run. |
| Seed confirmed gaps as regressions; demonstrate failure under deliberate breakage | Done | See "Before / after" below. |
| Publish screenshots and traces | Done | `ci.yml` job `e2e-cross-browser` uploads `e2e-cross-browser-evidence`: per-cell PNG + `axe-results.json`, and the screenshot + `trace.zip` of any failure or retry. |
| Stable required gate + broader nightly | Nightly done; **required promotion not done** | `.github/workflows/e2e-nightly.yml` (all engines, whole suite). `e2e-cross-browser` is advisory. Promotion needs the flake evidence below and is an owner/admin branch-protection action. |
| Flake budget < 1% | **Not yet measurable** | Tool and rule done (`web/scripts/check-e2e-flake.mjs`, `e2eFlake.mjs`). Local: 0 flaky of 370 runs (Chromium + Firefox, 5 repeats, retries off). A verdict needs ≥ 300 runs **per engine**, pooled across CI/nightly runs on the real runner, and WebKit has no data yet. |

### Before / after (deliberate breakage)

The three product fixes below were reverted, the app rebuilt, and the gate run on Chromium and
Firefox; then re-applied. Retries off.

| Seeded gap (real, not synthetic) | Before the fix | After |
|---|---|---|
| Session-card **Quick replies** chevron 20px wide on a hover pointer (WCAG 2.2 SC 2.5.8, 24px) | red on both engines (geometry assertion + axe `target-size`) | green |
| **Settings › Plugins** / **› MCP** text dimmed with `opacity-*`: 3.2–4.3:1 vs 4.5:1 (SC 1.4.3) | red on both engines (axe `color-contrast`) | green |
| Phone dispatch bar **More options**: `aria-label` replaced its content, so the "non-default selected" hint was never announced (SC 2.5.3) | red on both engines (axe `label-content-name-mismatch`) | green |

Totals: **10 failing tests before** (the 3 gaps trip 5 tests, on each of 2 engines), **370 / 370
passing after** (2 engines × 5 repeats). The gates were also shown to fail on purely synthetic breakage
(`seeded-defects.spec.ts`: overflow, 10px target, off-screen control, overlapping regions, clipped
text, plus the axe cases) — with the expectation inverted so the suite stays green while proving
the check can bite.

### Known limits of this evidence

- **WebKit was not run locally.** This host (Ubuntu 26.04) cannot run Playwright's WebKit build:
  its GStreamer 1.28 no longer ships a library that build links against. WebKit runs for the first
  time in CI. It is advisory, so a WebKit-only failure there is information, not a blocked merge —
  but it means "three engines" is configured, and verified on two.
- **Chromium and Firefox here are not the CI runner.** Fonts and rasterization differ; the
  assertions use 1px tolerances and token-driven checks rather than pixel equality for that reason.

### Sibling audit (what this change did *not* fix)

Found while doing the above; each is a follow-up, none is silently ignored.

- **27 files** still use `opacity-50/60/70` (some on non-text such as disabled controls). The gate
  only reaches the routes it scans; the pattern that failed contrast in Plugins/MCP can recur
  wherever it is on text. Route the text cases through `text-ink-muted`.
- **L4** (usage badge `aria-label` on a role-less `<span>`) is a real, accepted, tracked
  deviation — see the limitations doc.
- **34 of 36** top-level e2e specs each define their own `/api/**` interception. New specs use
  `fixtures/seed.ts`; migrating the old ones is a separate, mechanical change.
- **`ui/Switch` is the shared switch**, yet `PluginsSection` (native checkbox) and `McpServerList`
  (hand-rolled button) each roll their own.

### Not done here (and why)

- **Promoting the gate to a required check** — needs the per-engine flake evidence above, then an
  owner/admin change to branch protection. An agent does not edit branch protection.
- **Pixel-diff baselines** — see the table; revisit once a baseline can be generated on the CI
  image (a `workflow_dispatch` that regenerates and commits them).

## G15 — Usability baseline (#1413)

Not started. Human-subjects research; out of scope for an agent (E5 spec §6).

## G16 — Shared page and token adoption (#1414)

Not recorded here. The E5 plan (§WS-4) records that the audit's four `dark:` hits were false
positives and that the work is substantially met by #993 and finish-line row 7.

## G17 — Maintenance cost and delivery stability (#1415)

Not started. The cost half is blocked on an owner-supplied loaded hourly rate (E5 spec §7 P6).
