# Spec — ViberPit UI E3: Browser trust, identity and kiosk privilege

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e3-browser-trust-identity-and-kiosk-privilege` |
| **Stage** | Design (propose-only; this artifact merges at a human gate) |
| **Epic** | [steiner385/viberpit#1396](https://github.com/steiner385/viberpit/issues/1396) — part of review initiative #1393 |
| **Children** | G10 [#1408](https://github.com/steiner385/viberpit/issues/1408) · G11 [#1409](https://github.com/steiner385/viberpit/issues/1409) · G12 [#1410](https://github.com/steiner385/viberpit/issues/1410) |
| **Specced against** | `4aa53194` (master, 2026-09-11) |
| **Audit reviewed** | `07c02a99` (2026-09-07) |
| **Governing principle** | Constitution v1.2.0 **IV** (Security First) with V (≥85% coverage on auth paths, explicit negative cases), VI (single source of truth) and VII (vetted libraries) |

## 0. Provenance — read this first

**There is no `intent.md` for this initiative, and this spec was not written from one.**

The Design prompt inlined `docs/sdlc/epic-viberpit-ui-e3-browser-trust-identity-and-kiosk-privilege/intent.md`
as its upstream input. The file does not exist on `origin/master`, on any remote branch, or in the
initiative's artifact index. The initiative's `origin` is `epic-adoption` (`originRef`
`steiner385/viberpit#1396`): it entered the pipeline at Design, so the intake stage that writes
`intent.md` never ran. This is the same structural gap the E1 spec (#1578, §0 and P6) and the E2 spec
(#1579, PC-5) documented; it is not re-filed here.

**The upstream intent used here is epic #1396 plus its three sub-issues**, cross-checked against the
audit's local gap register (`~/Documents/ViberPit-UI-Review-2026-09-07/gap-register.json`, entries
G10–G12), which carries one fact the GitHub issues omit: **G10 is `depends: ["G11"]`** — the kiosk
principal is built on top of the origin boundary, not beside it. All inputs are read-only and were
not modified.

### Every current-state claim below was re-verified against `4aa53194`

The audit reviewed `07c02a99`. Master has moved (finish-line rows 4–7c), but every scope path the
three issues cite still exists. What changed is not the paths but the **evidence**, which is
stronger than the audit stated in three places and weaker in one:

| Audit statement | Status on `4aa53194` |
|---|---|
| "`require_ws_identity` has no Origin allowlist" | **Understated.** No code anywhere in `server/` reads the `Origin` header; there is no CORS middleware, no CSRF token, no `Sec-Fetch-*` check, and no security response header (CSP, `X-Frame-Options`, HSTS, `Referrer-Policy`, `X-Content-Type-Options`) in either the app or the nginx vhosts. |
| "query parameters do not restrict direct APIs" | **Understated.** `grep -rni kiosk server/ src/` is empty. Beyond the API, the one page that is kiosk-safe (`pipeline`) renders a live mutating **Fix** button (`web/src/pipeline/PrRow.tsx:263-266`, passed unconditionally from `PipelinePage.tsx:143`), and the shell **opens the full-privilege session WebSocket in kiosk** (`AppShell.tsx:472` → `useAppBanners.tsx:33` → `useInputHold.ts:13` → `getSessionSocket()`). |
| "WebAuthn is an unwired scaffold raising `NotImplementedError`" | **Confirmed.** `server/auth/webauthn.py` is 44 lines; nothing imports it; no credential table; no client code; the flag is read as bare `WEBAUTHN_ENABLED`, while `README.md:182` documents `CCSWITCH_WEBAUTHN_ENABLED` (docs/code mismatch). The `webauthn` library **is** already declared in `pyproject.toml:27`. |
| "A page at 127.0.0.1:4187 opened ws://127.0.0.1:8787/ws" | **Confirmed as a local-edge fact, and narrower than it sounds.** In prod the edge is loopback-bound behind nginx (`deploy/viberpit-tailnet.nginx`), which blanks the identity headers and forwards `X-Real-IP`; identity comes from `tailscale whois`. A hostile page must therefore run in a browser on a tailnet-authorized device. That is exactly the browser the operator uses all day, so the finding stands. |

Two sibling facts found during verification are recorded in [§7 P9](#p9-sibling-findings-outside-this-epics-scope)
rather than silently folded into scope.

---

## 1. Summary

Three findings share one root: **the server trusts the network peer and nothing else**. Every
browser on a tailnet-authorized device holds full privilege by virtue of its source address; the
cookie is bookkeeping (`server/auth/middleware.py:62-63`); kiosk is a client-side costume; and the
one app-level authenticator the constitution requires is a stub.

This spec defines three server-enforced boundaries, in dependency order:

1. **G11 — an origin boundary.** Ambient-identity callers (tailnet, `whois`, LAN guest) must present
   an allowed browser `Origin` on every WebSocket upgrade and every state-changing HTTP request.
   Bearer-token callers are exempt, because a bearer is not ambient and cannot be forged by a
   cross-site page. Plus a fixed set of security response headers.
2. **G10 — a display principal.** A revocable, operator-minted **display token** is exchanged for a
   **display-scoped session cookie**. The scope lives in `AppSession`, is attached to every request,
   and is enforced by one route policy: display scope reads an allowlist of summary endpoints and
   nothing else, and is refused every WebSocket. The client's `?kiosk=1` layout hint stays, but the
   server scope is the truth it must follow.
3. **G12 — a real exposure policy and a working passkey path.** One `CCSWITCH_EXPOSURE` tier
   setting validated fail-closed at startup; passkey (WebAuthn) registration, login, revocation and
   recovery built on the declared `webauthn` library; and the anonymous `lan@local` branch
   reconciled with Principle IV — recommended for removal, decided at the gate ([§7 P2](#p2-governance--the-anonymous-lan-branch-contradicts-principle-iv)).

The three share **one configuration source** — the allowed-origins list — which G11 enforces, G12's
WebAuthn relying-party check consumes, and G10's exchange endpoint validates against. It is defined
once, in `server/config.py`, and never re-derived.

Constitution VI applies with force here: the repo already has a scoped-token model
(`AssistantToken.scope: "read" | "act"`), a route allowlist (`server/assistant/policy.py:route_allowed`),
a per-identity rate limiter (`server/api/ratelimit.py`), a peer-trust predicate
(`identity.py:_peer_is_trusted`) and a loopback check (`identity.py:host_is_loopback`). **Every
requirement below extends one of these; none introduces a parallel copy.**

---

## 2. Functional behavior

### G11 — Validate browser origins at WebSocket and mutation boundaries

**Current state (verified).** Five WebSocket routes exist. Three are browser-facing and guarded
only by `require_ws_identity` (`/ws` at `server/bridge/ws.py:421`, `/ws/vnc` at
`server/bridge/vnc_ws.py:147`, `/ws/stt` at `server/stt/ws.py:57`); two are daemon IPC (`/events`,
`/vnc` under `server/ipc/`) gated by the transport (Unix socket permissions or mTLS,
`server/ipc/serve.py:45-61`) and are **out of scope**. `IdentityMiddleware` (`middleware.py:227`)
never runs on a WebSocket upgrade, so `require_ws_identity` is the sole enforcement point for WS.

The web client opens both sockets with a bare relative path and no credential
(`web/src/ws/client.ts:167`, `web/src/lib/useServerDictation.ts:74`); mutations send only
`Content-Type` (`web/src/api/client.ts:40-44`). Authentication is therefore **ambient**: whatever
the peer address resolves to. Because a browser attaches ambient identity to any request a page
initiates, a page on any origin can open `wss://<edge>/ws` or `POST /api/sessions/<id>/prompt` from
the operator's browser and be authenticated as the operator. `SameSite=Strict` on the
`ccswitch_session` cookie does not help: the cookie is not the credential.

Non-browser callers that must keep working, and what they present:

| Caller | Credential | Sends `Origin`? |
|---|---|---|
| `ccswitch ask` / `ccswitch mcp` (`src/ccswitch/assistant_ask.py:112`, `assistant_mcp.py:31`) | `Bearer ccas2:…` | No |
| In-process assistant tools (`server/assistant/client.py:33`) | `Bearer ccas1:…`, loopback only | No |
| `scripts/smoke_assistant.py:109` | forged `Tailscale-User-Login` from loopback | No |
| Deploy health probe (`server/deploy/steps.py:620`) | none; `/healthz` is exempt | No |
| Vite dev server proxy (`web/vite.config.ts:43-49`, `changeOrigin: true`) | ambient (loopback) | **Yes** — the browser's `http://localhost:5173`, forwarded unchanged (`changeOrigin` rewrites `Host`, not `Origin`) |

**Target behavior.**

- **FR-G11.1 One allowed-origins source.** A new `Settings.allowed_origins`
  (`CCSWITCH_ALLOWED_ORIGINS`, comma-separated `scheme://host[:port]`) is the single list. Its
  **effective** value is the configured list plus loopback origins for the configured port
  (`http://127.0.0.1:<port>`, `http://localhost:<port>`) and, when `CCSWITCH_DEV=1`, the Vite dev
  origins. The effective list is logged once at startup. No other module derives its own notion of
  "allowed origin" — G12's WebAuthn relying-party origins and G10's exchange endpoint read this
  setting.
- **FR-G11.2 One check, two call sites.** A single function in `server/auth/` (adjacent to
  `verify_identity`) decides whether a request's browser provenance is acceptable. It is called
  from `require_ws_identity` for every browser-facing WebSocket upgrade and from
  `IdentityMiddleware.dispatch` for every HTTP request whose method is not `GET`/`HEAD`/`OPTIONS`.
  Route handlers do not repeat the check.
- **FR-G11.3 Decision table.** Let *ambient* mean the identity was resolved by
  `verify_identity` (header, `whois` or LAN guest), and *bearer* mean a `ccas1:`/`ccas2:` token
  authenticated the request.

  | Caller class | `Origin` present and allowed | `Origin` present and not allowed | `Origin: null` | `Origin` absent |
  |---|---|---|---|---|
  | Ambient, WebSocket upgrade | accept | **close 1008** | close 1008 | **close 1008** |
  | Ambient, mutating HTTP | accept | **403 `ORIGIN_FORBIDDEN`** | 403 | **403** |
  | Ambient, safe HTTP (`GET`/`HEAD`/`OPTIONS`) | accept | accept (same-origin policy protects the response) | accept | accept |
  | Bearer (`ccas1:`/`ccas2:`), any | accept | accept | accept | accept |

  An absent `Origin` on an ambient mutation is rejected, not tolerated: browsers always send
  `Origin` on non-`GET` fetches and on every WebSocket handshake, so an ambient caller with no
  `Origin` is either a non-browser client (which must use a bearer) or a browser in a forged
  configuration. The `smoke_assistant.py` script is a non-browser ambient caller and **must be
  updated** to send an allowed `Origin` (it is a test harness, not a supported client).
- **FR-G11.4 Defense in depth, not a second policy.** When `Sec-Fetch-Site` is present and equals
  `cross-site`, an ambient mutation or upgrade is rejected regardless of `Origin`. This is a second
  signal inside the same function, not a second checkpoint.
- **FR-G11.5 Security response headers**, set by the app for every response (so the loopback dev
  server and any future ingress get them, not just nginx):
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
  `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`.
  `Strict-Transport-Security` is set at nginx only (it is meaningless on the plain-HTTP loopback
  bind). A full CSP (script/style sources) is a **non-goal** here — see [§6](#6-non-goals) and
  [§8 Q5](#q5-full-content-security-policy-now-or-follow-on).
- **FR-G11.6 Ingress contract.** The nginx vhosts (`deploy/viberpit-tailnet.nginx`,
  `deploy/ccswitch-tailnet.nginx`) must forward `Origin` unchanged (they already do — nothing
  rewrites it) and must **not** be relied on to enforce origin. The deploy runbook records the HSTS
  directive and the one-line statement that origin enforcement lives in the app.
- **FR-G11.7 Audit.** Every rejection is an audit event carrying the peer, the offending origin
  (bounded length, never the identity string — the same redaction rule `identity.py:82-86` applies
  to the asserted header) and the surface (`ws`, `ws/stt`, `ws/vnc`, `http`).

**Scope.** `server/auth/identity.py` or a sibling module (the check) · `server/auth/middleware.py`
(both call sites) · `server/config.py` (setting) · `server/app.py` (headers middleware; startup
log) · `deploy/*.nginx` + `deploy/README.md` (HSTS, contract note) · `scripts/smoke_assistant.py`
(send `Origin`) · `server/tests/test_auth*.py`, `test_ws_external_token.py`, `test_stt_ws.py`
(extend; do not add a parallel WS-auth test file).

### G10 — Server-enforced read-only identity for unattended kiosks

**Current state (verified).** Kiosk is `?kiosk=1&pages=…&cycle=N&theme=…` parsed once at
`AppShell` mount (`web/src/shell/kiosk.ts:53-61`). It hides the rail, list, dock and drawer
(`AppShell.tsx:123-129`), filters banners to `danger`, gates routes to `showInKiosk` sections
(`KioskPageGuard`, `AppShell.tsx:221-227`; only `pipeline` qualifies, `navDefs.ts:86-115`), and
provides `InteractionContext=false`, which exactly **four** components consult (`Dialog`,
`PageHeader`, `StatusStrip`, `PrRow.expandable`). `navDefs.ts:32-54` already states the design's
own limit: "`showInKiosk` gates a SECTION, not a scope."

Server-side there is no kiosk concept, no scope on a browser identity, and no `Identity`
type — identity is a bare `str` everywhere. The **only** scope mechanism in the codebase is the
`ccas2:` external token (`AssistantToken.scope`, `server/db/models.py:339`; enforced at
`middleware.py:213-215` for HTTP and `:438-445` for WS via `route_allowed`). Nothing in the browser
path can obtain one.

Concretely, on `4aa53194` a wall display at `/pipeline?kiosk=1`:
- renders a live **Fix** button on every fixable PR row (`PrRow.tsx:263-266`) — a mutation
  (`useFixPr`, `POST /api/sessions/<id>/fix-pr`) reachable by any passer-by;
- opens the full-privilege `/ws` socket (via `useAppBanners` → `useInputHold`);
- and, trivially, becomes a full operator console the moment someone deletes `?kiosk=1`.

**Target behavior.**

- **FR-G10.1 A display principal exists and is server-enforced.** A request's authorization is a
  pair `(identity, scope)` where `scope ∈ {"full", "display"}`. `scope` is attached to
  `request.state` / `websocket.state` by the same code that attaches `identity`, is stored on
  `AppSession` (new nullable column `scope`, default `"full"`), and is returned by
  `GET /api/auth/me`. No route may infer scope from a URL parameter, a header the client controls,
  or the SPA's mode.
- **FR-G10.2 Display tokens extend the existing token model.** A display token is an
  `AssistantToken` row with `scope = "display"` — the same table, hashing, expiry and revocation
  machinery as `ccas2:` (§4 lists the exact deltas). It is minted by a **full-scope** identity from
  the browser, shown once, and carries an operator-chosen expiry (default 30 days, max 365) and a
  label ("Lobby wall"). Revocation is immediate: every request re-validates the token row exactly
  as `_validate_user_token` does today (`middleware.py:144-185`), so a revoked token's sessions
  fail on their next request.
- **FR-G10.3 Exchange, not bearer.** A display cannot set `Authorization` on a navigation, an
  `EventSource` or a WebSocket, so the token is exchanged: the display opens
  `/kiosk?token=<display token>` (client route), the SPA `POST`s the token to
  `/api/kiosk/session`, and the server answers with a **display-scoped `AppSession` cookie**
  (`ccswitch_session`, same name, `scope="display"`, `expires_at ≤ token.expires_at`, row records
  `token_id`) and a `303` back to the first kiosk page with the token stripped from the URL. The
  token is never logged (audit redaction already covers `ccas` prefixes; the display prefix joins
  that rule). See [§7 P3](#p3-security--a-credential-in-a-url) for the URL-carriage risk.
- **FR-G10.4 Narrower scope wins.** If a request carries a display-scoped session cookie, the
  request is display-scoped **even if** the ambient identity would be full. An operator who opens a
  display link on their own laptop gets a read-only app until they end the display session
  (`POST /api/auth/logout`, which already revokes the row and clears the cookie). The UI names this
  state plainly ("Display mode — sign out to resume"). Ambient identity is **not required** for a
  display request: a tagged tailnet node or a LAN screen with no user identity is admitted on the
  token alone — that is the point of the principal. See [§8 Q2](#q2-should-display-scope-require-an-ambient-identity-as-well).
- **FR-G10.5 One route policy, extended.** `server/assistant/policy.py:route_allowed` gains a
  display allowlist alongside `ASSISTANT_ROUTE_ALLOW` and `OPERATOR_ROUTE_ALLOW`, and
  `IdentityMiddleware` consults it for display-scoped requests exactly as it does for `ccas2:`
  tokens. The initial allowlist is **summary reads only**:
  `GET /api/pipeline/snapshot`, `GET /api/pipeline/events`, `GET /api/config`, `GET /api/auth/me`,
  `POST /api/auth/logout`, plus static assets. Everything else — including every `/api/sessions*`,
  `/api/accounts*`, `/api/settings*` and the token-minting routes — is `403 DISPLAY_SCOPE_FORBIDDEN`.
  Adding a page to kiosk means adding its reads to this list **and** setting `showInKiosk`; the
  two are reviewed together.
- **FR-G10.6 No sockets for displays.** `require_ws_identity` refuses a display-scoped session on
  `/ws`, `/ws/stt` and `/ws/vnc` (close 1008), mirroring the existing refusal of `read`-scope
  `ccas2:` tokens (`middleware.py:440-443`) and for the same reason: `/ws` has no per-frame
  allowlist. The client stops opening the socket in kiosk (`useAppBanners`'s `useInputHold` call
  becomes conditional on scope), so a display never even attempts it.
- **FR-G10.7 Minimized detail.** Display-scope responses carry no account identifiers (login
  emails), no session goals/prompts and no tailnet hostnames. `GET /api/pipeline/snapshot` and the
  `events` stream are audited for these fields and, where present, are projected for display scope
  at the endpoint (not in the client). `GET /api/auth/me` for a display returns
  `{"identity": "display:<label>", "scope": "display", "authMode": "display"}` — never the
  minting operator's email. See [§8 Q3](#q3-what-does-a-wall-display-get-to-show).
- **FR-G10.8 The client follows the server.** The shell derives `interactive = false` from
  **either** `?kiosk=1` **or** `scope === "display"` (from `GET /api/auth/me`), so a display
  session with the parameter stripped still renders as kiosk, and an operator adding `?kiosk=1`
  still gets the layout without any privilege change. The two ungated surfaces found in §0 are
  fixed in the same child: the **Fix** button is not rendered when not interactive, and the socket
  is not opened. `navDefs.ts`'s "verified read-only" comment is updated to say that the
  verification is now the server's allowlist, not a component audit.
- **FR-G10.9 Management surface.** Settings (full scope only) lists display tokens with label,
  created, expires, last used and a **Revoke** action, reusing the existing `/api/assistant/tokens`
  list/revoke routes filtered by scope (or a thin `/api/kiosk/tokens` alias over the same handlers
  — one implementation either way; the Plan stage picks the URL).

**Scope.** `server/db/models.py` (+ migration) · `server/auth/middleware.py` (scope attach, WS
refusal) · `server/assistant/policy.py` (display allowlist) · `server/assistant/api.py` or a new
`server/api/kiosk.py` (mint/exchange, one implementation) · `server/api/auth.py` (`/me` shape) ·
`server/api/pipeline.py` (display projection) · `web/src/shell/kiosk.ts`, `AppShell.tsx`,
`useAppBanners.tsx`, `InteractionContext.tsx`, `navDefs.ts` · `web/src/pipeline/PrRow.tsx`,
`web/src/features/pipeline/PipelinePage.tsx` · `web/src/api/` (auth/me hook, kiosk session) ·
Settings page (token list).

### G12 — Close the strong-authentication and exposure-policy gap

**Current state (verified).**

*Exposure.* There is no exposure setting. The only startup guard (`server/app.py:184-209`) refuses
to boot when the tailnet allowlist is **empty** on a non-loopback bind, unless `CCSWITCH_DEV=1`.
Consequences: a non-empty allowlist on `0.0.0.0` starts silently; `CCSWITCH_ALLOW_UNAUTHENTICATED=true`
then bypasses that allowlist for guests (`identity.py:99-104`, by design) and mints `lan@local` a
**full-scope** identity with `/ws` prompt rights; `CCSWITCH_DEV=1` overrides the guard wholesale and is
set in ~15 test files; and the guard's `_is_loopback` is a three-string literal comparison rather
than `identity.host_is_loopback()`, whose docstring claims to be "the one such check in the
codebase" (`identity.py:34`). `GET /api/auth/me` returns a hardcoded `"authMode": "tailscale"`.

*Strong auth.* `server/auth/webauthn.py` is four stubs raising `NotImplementedError` behind a
misnamed flag; no routes, no credential model, no client code. The `webauthn` (py_webauthn) library
is declared in `pyproject.toml:27`. The session cookie exists (`itsdangerous.TimestampSigner`,
HttpOnly, `SameSite=Strict`, `Secure` unless `CCSWITCH_DEV_INSECURE_COOKIE=1`, 24 h absolute,
`AppSession` row) but is bookkeeping, since identity is re-resolved ambiently on every request. A
per-identity rate limiter exists (`server/api/ratelimit.py`, #1185).

*Governance.* Principle IV says "No deployment is ever reachable by an unidentified caller" and
"an app-level strong-authentication path … MUST exist and be enable-able from day one". Both are
currently false on master. `.specify/memory/constitution.md` is write-protected by managed policy
in this environment; an amendment is an owner PR, not something this epic may do.

**Target behavior — exposure policy.**

- **FR-G12.1 One exposure setting.** `Settings.exposure` (`CCSWITCH_EXPOSURE`) with values
  `loopback | tailnet | lan | public`, default `tailnet`. It replaces the ad-hoc guard in
  `app.py:184-209` with one validator (in `server/config.py` or a sibling `server/auth/exposure.py`)
  that runs at startup **and** is the single place the rules below live.
- **FR-G12.2 Fail-closed tier rules.**

  | Tier | Bind | Identity | Requires | Refuses |
  |---|---|---|---|---|
  | `loopback` | must satisfy `host_is_loopback()` | any (peer is the operator) | — | non-loopback bind |
  | `tailnet` | loopback (behind nginx/`tailscale serve`) or a tailnet address | tailnet header/`whois` | non-empty `CCSWITCH_TAILNET_ALLOWLIST`; `CCSWITCH_ALLOWED_ORIGINS` set | `CCSWITCH_ALLOW_UNAUTHENTICATED`; bind to `0.0.0.0` / a non-tailnet address |
  | `lan` | any private address | tailnet **or passkey** | passkey enabled with ≥1 registered credential; allowlist; origins; `Secure` cookies | `CCSWITCH_ALLOW_UNAUTHENTICATED` (see P2); `CCSWITCH_DEV_INSECURE_COOKIE` |
  | `public` | any | passkey (tailnet identity also honoured if present) | everything in `lan` **plus** HSTS attested in the ingress config, login rate limit on, `CCSWITCH_SESSION_SECRET` set (no ephemeral fallback) | any missing prerequisite |

  A violated rule is a `RuntimeError` at startup with the tier, the rule and the fix in the
  message. `CCSWITCH_DEV=1` may relax **only** the `loopback` and `tailnet` tiers, and only to a
  logged warning; it has no effect on `lan` or `public`.
- **FR-G12.3 `lan@local` is reconciled, not silently kept.** The recommended resolution is to
  **delete** `CCSWITCH_ALLOW_UNAUTHENTICATED` and `OPEN_LAN_IDENTITY`: the `lan` tier's answer to
  "a device without Tailscale" is a passkey login, which satisfies Principle IV without amendment.
  If the owner instead keeps an anonymous option, it becomes a constitution amendment PR **first**
  and, at most, admits anonymous callers at **display scope** (G10), never full. The gate decides —
  [§7 P2](#p2-governance--the-anonymous-lan-branch-contradicts-principle-iv). Whatever the decision,
  the tests pinning the current branch (`test_auth.py:93-132`,
  `test_auth_identity_peer_trust.py:54,83`) are rewritten to pin the decided behavior, not deleted.
- **FR-G12.4 One loopback predicate.** `app.py`'s literal comparison is replaced by
  `identity.host_is_loopback()`. Sibling sweep: every other `"127.0.0.1"` string comparison in
  `server/` is audited in the same PR.
- **FR-G12.5 `authMode` is real.** `GET /api/auth/me` returns the mode that actually authenticated
  the request — `tailscale | passkey | display | bearer` — derived from the same state the
  middleware set, not a literal.

**Target behavior — passkey (WebAuthn) path.**

- **FR-G12.6 Library, not hand-rolled.** Registration and authentication ceremonies use
  `py_webauthn` (`webauthn` on PyPI, already declared). The relying-party id is the host of the
  configured origin and the expected origins are `Settings.allowed_origins` (FR-G11.1) — one
  source, no second list.
- **FR-G12.7 Flag and routes.** `CCSWITCH_WEBAUTHN_ENABLED` (in `Settings`; the bare
  `WEBAUTHN_ENABLED` read in `webauthn.py:15` is deleted). When enabled, the router mounts:
  `POST /api/auth/webauthn/register/begin`, `…/register/complete`,
  `POST /api/auth/webauthn/login/begin`, `…/login/complete`,
  `GET /api/auth/credentials`, `DELETE /api/auth/credentials/{id}`.
  When disabled, none of these exist (404), the stubs are gone, and nothing raises
  `NotImplementedError` anywhere in `server/auth/`.
- **FR-G12.8 Identity binding and bootstrapping.** A passkey is bound to an identity string that
  is **the same string the tailnet path produces** (a login email), so `CCSWITCH_TAILNET_ALLOWLIST`
  governs both paths. The **first** credential for an identity can be registered only by a request
  that is already **full-scope and tailnet-authenticated** as that identity (an operator on the
  tailnet enrols their own passkey). Subsequent credentials may be added from a passkey-authenticated
  session. There is no self-service enrolment for an unknown identity.
- **FR-G12.9 The cookie becomes a credential.** A passkey login mints an `AppSession` with
  `auth_method = "passkey"`. For such a session the signed cookie **is** the authenticator: the
  middleware accepts it without an ambient identity, requires `Secure` (FR-G12.2), keeps the 24 h
  absolute expiry, adds an **idle timeout** (`CCSWITCH_SESSION_IDLE_S`, default 8 h, touched on
  each request), and never falls back to the ephemeral per-process secret in `lan`/`public`
  (`_signing_secret`'s last branch, `middleware.py:83-91`, becomes a startup refusal there).
  `POST /api/auth/logout` revokes it as today. See [§8 Q4](#q4-session-lifetime-for-passkey-sessions).
- **FR-G12.10 Login is rate-limited** through `server/api/ratelimit.py` — per peer address and per
  claimed identity — with a lockout window and an audit event on every failure. No second limiter.
- **FR-G12.11 Revocation and recovery.** `DELETE /api/auth/credentials/{id}` revokes one passkey
  and every `AppSession` minted by it. Recovery from a lost passkey is the tailnet path
  (FR-G12.8): sign in over Tailscale, revoke, re-enrol. There is **no** email/SMS/recovery-code
  channel in this epic — see [§7 P5](#p5-security--passkey-recovery-is-the-tailnet-path-only).
- **FR-G12.12 Client.** A `/login` route (rendered only when `authMode` allows passkey and the
  request is unauthenticated) drives `navigator.credentials.get`; Settings → Security lists
  credentials with enrol/revoke. Unauthenticated requests to the SPA in `lan`/`public` tiers land
  on `/login`, not on a 401 JSON body.
- **FR-G12.13 ASVS record.** The PR records which OWASP ASVS 4.0 controls in V2 (authentication),
  V3 (session management) and V13.2/V14.4 (API/HTTP security headers) this epic satisfies, and at
  which level — [§8 Q6](#q6-asvs-target-level) fixes the level.

**Scope.** `server/config.py` · `server/app.py` (guard removal, router mount, headers) ·
`server/auth/exposure.py` (new, single) · `server/auth/webauthn.py` (rewritten, no stubs) ·
`server/auth/middleware.py` · `server/api/auth.py` · `server/api/ratelimit.py` (consume) ·
`server/db/models.py` (+ migration: `webauthn_credentials`, `app_sessions.auth_method`,
`app_sessions.last_seen_at`) · `web/src/` (`/login`, Settings → Security) · `deploy/README.md`
(tier runbook) · `docs/product/features/tailnet-identity-auth.md` (updated in place, not a second
doc) · `README.md:182` (flag name).

### Dependency order

`G11 → G10` is a native blocked-by link (gap register `depends: ["G11"]`): the display exchange
endpoint is an ambient-identity mutation and needs FR-G11.3 in place, and `require_ws_identity`
is edited by both. `G12` is independent of `G10` but **shares `allowed_origins` with `G11`**; the
Plan stage should land FR-G11.1 first as its own small PR so G11 and G12 do not both create the
setting. The Plan stage re-reads the native links at decomposition time rather than trusting this
paragraph.

---

## 3. Acceptance criteria

Verification-first: every criterion is a test that **fails on `4aa53194` and passes after**. Auth
paths must reach ≥85% coverage with explicit negative and abuse cases (constitution V). Per
#1409: **no live prompt is executed to test any of this** — mutation tests target routes whose
handlers are stubbed or use `cancelQueued`/`detach`-class frames, never `prompt`.

### G11
- **AC-G11.1** For each of `/ws`, `/ws/stt`, `/ws/vnc` with a valid ambient identity: an allowed
  `Origin` opens; a disallowed `Origin`, `Origin: null`, and **no** `Origin` are each closed with
  1008 **before** `accept()` (use the `_closed_before_accept` helper from
  `test_ws_external_token.py:59-69`). Assert the close code, not merely "no exception".
- **AC-G11.2** With a valid `ccas2:` `act` token and no `Origin`: `/ws` opens (CLI preserved).
  With a `ccas1:` token from loopback and no `Origin`: an allowlisted `POST` succeeds.
- **AC-G11.3** Ambient mutation on a real route with a stubbed handler (e.g.
  `DELETE /api/sessions/<id>/queue`, never `/prompt`): allowed `Origin` →
  handler reached; disallowed / `null` / absent → `403 ORIGIN_FORBIDDEN` and the handler is **not**
  reached (assert via a spy on the handler, not on the status alone). Ambient `GET` with a
  disallowed `Origin` → 200.
- **AC-G11.4** `Sec-Fetch-Site: cross-site` with an allowed `Origin` on an ambient mutation → 403.
- **AC-G11.5** Effective allowed origins: configured list ∪ loopback:<port>; with
  `CCSWITCH_DEV=1` also the Vite dev origins; without it, `http://localhost:5173` is rejected.
- **AC-G11.6** Every HTTP response (including `/healthz`, a 401 and a static asset) carries the
  four FR-G11.5 headers with the exact values; HSTS is **absent** from app responses and present
  in the nginx vhost files (assert the file contains the directive).
- **AC-G11.7** Each rejection writes one audit event with surface + peer + bounded origin and
  **no** identity string.
- **AC-G11.8** `grep -rn 'headers.get("origin")\|headers\["origin"\]' server/` finds exactly one
  call site (the FR-G11.2 function). `scripts/smoke_assistant.py` passes against a server with
  enforcement on.

### G10
- **AC-G10.1** Mint a display token as a full-scope identity; exchange it at
  `POST /api/kiosk/session` from an allowed origin: response sets a `ccswitch_session` cookie whose
  `AppSession` row has `scope="display"`, `token_id` set, `expires_at ≤ token.expires_at`; the
  redirect location contains no `token=`.
- **AC-G10.2** With that cookie and **no** ambient identity (peer `203.0.113.7`, no headers,
  `whois` stubbed to None): `GET /api/pipeline/snapshot`, `GET /api/pipeline/events` (first SSE
  frame), `GET /api/config`, `GET /api/auth/me` → 200. Every other route in the app's route table
  (enumerate `app.routes`, exclude the allowlist and static) → 403 `DISPLAY_SCOPE_FORBIDDEN` for
  the method it declares. This is a table-driven test over the real router, not a sample.
- **AC-G10.3** With the display cookie: `/ws`, `/ws/stt`, `/ws/vnc` each close 1008 before accept.
- **AC-G10.4** Display cookie **plus** a valid full ambient identity on the same request → scope is
  `display` (narrower wins); `GET /api/auth/me` reports `scope: "display"`, `authMode: "display"`,
  and an identity of the form `display:<label>` — the minting operator's email appears nowhere in
  the body.
- **AC-G10.5** Revoke the token → the next request with the cookie → 401; the `AppSession` row is
  revoked. Expire the token (clock) → same. Exchange a revoked or expired token → 401, no cookie
  set, one audit event.
- **AC-G10.6** Exchange from a disallowed origin → 403 (G11 applies to the exchange itself).
- **AC-G10.7** Display-scope `snapshot` and `events` payloads contain no `@`-bearing identity
  string, no session goal/prompt text and no `.ts.net` hostname (assert by scanning the serialized
  body against seeded fixtures that contain all three in full scope).
- **AC-G10.8** Web: render `/pipeline` with `scope: "display"` and **no** `?kiosk=1` — the rail,
  dock and Fix button are absent and no `WebSocket` constructor is called (spy). Render with
  `?kiosk=1` and `scope: "full"` — same layout, `Fix` absent, socket not opened. Render with
  neither — `Fix` present.
- **AC-G10.9** Settings lists the token with label/expiry/last-used; Revoke calls the existing
  revoke route; the list is absent for a display-scoped session.
- **AC-G10.10** Audit log for mint, exchange, revoke never contains the token; the redaction test in
  `server/tests/` that already covers `ccas` prefixes is extended to the display prefix.

### G12
- **AC-G12.1** Startup matrix, table-driven over every row of FR-G12.2: each "Refuses" cell raises
  `RuntimeError` whose message names the tier and the rule; each satisfied row starts.
  `CCSWITCH_DEV=1` converts only `loopback`/`tailnet` refusals into a single warning and leaves
  `lan`/`public` refusals fatal.
- **AC-G12.2** `host_is_loopback` is the only loopback predicate: `grep -rn '"127.0.0.1"' server/
  --include=*.py` outside `config.py` defaults and tests returns nothing that is a comparison.
- **AC-G12.3** Whichever P2 outcome the gate chooses is pinned: **(removal)** no
  `allow_unauthenticated` setting exists, `OPEN_LAN_IDENTITY` is gone, and an unidentified caller
  gets 401 in every tier; **(display-only)** an unidentified caller in `lan` gets `scope="display"`
  and AC-G10.2/3 hold for it.
- **AC-G12.4** Passkey ceremony end to end with a software authenticator (`py_webauthn` test
  vectors or `soft-webauthn`): register/begin+complete as a tailnet-authenticated full identity →
  credential row; login/begin+complete with no ambient identity → `AppSession`
  `auth_method="passkey"` and a `Secure`, `HttpOnly`, `SameSite=Strict` cookie; subsequent
  request with cookie only → 200 with `authMode: "passkey"`.
- **AC-G12.5** Negative: register/begin from a non-tailnet or display-scoped caller → 403; a
  challenge replayed → 400; an assertion for a revoked credential → 401; an assertion whose origin
  is not in `allowed_origins` → 401; a sign-count regression → 401 + audit; tampered cookie → 401.
- **AC-G12.6** Rate limit: N failed logins from one peer within the window → 429 with
  `Retry-After`; one audit event per failure; the limiter instance is `server/api/ratelimit.py`'s
  (assert by import identity, not behavior only).
- **AC-G12.7** Idle timeout: a passkey session untouched for `CCSWITCH_SESSION_IDLE_S` + 1 → 401;
  touched within → 200 and `last_seen_at` advanced. Absolute 24 h still enforced.
- **AC-G12.8** `DELETE /api/auth/credentials/{id}` → the credential row is revoked and every
  `AppSession` minted by it is revoked; the tailnet path still authenticates the same identity
  (recovery).
- **AC-G12.9** `lan`/`public` with no `CCSWITCH_SESSION_SECRET`/`MASTER_KEY` → startup refusal;
  `tailnet` → the existing one-shot warning.
- **AC-G12.10** With `CCSWITCH_WEBAUTHN_ENABLED` unset, every `/api/auth/webauthn/*` route is 404
  and `grep -rn NotImplementedError server/auth/` is empty.
- **AC-G12.11** Web: unauthenticated navigation in a passkey-enabled tier renders `/login`;
  Settings → Security lists and revokes credentials; the e2e suite covers login with a virtual
  authenticator (Playwright `CDPSession` `WebAuthn.addVirtualAuthenticator`).

### Gates applying to every child
- **AC-X.1** `lint-and-test` — all four steps (`ruff check src/ server/`, `mypy server/
  --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`).
- **AC-X.2** `frontend` — `npm run typecheck`, `npm run lint` (`--max-warnings 0`), the **full**
  `npm run test`, `npm run build`.
- **AC-X.3** New models/columns: nullable columns (auto-reconciled) or a migration chained off the
  newest revision (`0035_error_signatures` as of this spec); `_REQUIRED_EXTRAS` in
  `test_postgres_compat.py` and, where applicable, `RECONCILE_EXEMPT` gain entries; the Postgres
  suite is run with `CCSWITCH_TEST_PG_URL` set before the PR is marked ready.
- **AC-X.4** `db_utc_now()` for every timestamp; no `func.now()`.
- **AC-X.5** Sibling audit performed and recorded; replaced code (the `app.py` guard, the
  `webauthn.py` stubs, the bare `WEBAUTHN_ENABLED` read, `lan@local` if removed) **deleted in the
  same PR**.
- **AC-X.6** Sanitized before/after evidence attached — seeded fixture data only; no account
  identifiers, tailnet hostnames or session goals ([§7 P8](#p8-data-handling--evidence-sanitization)).
- **AC-X.7** Live smoke on the deployed tailnet edge after each child lands (constitution III): a
  cross-origin page cannot open `/ws`; a display link renders and cannot mutate; a passkey login
  round-trips. Recorded in the PR, not just the test suite.

---

## 4. Data and API surface

### Configuration (all read once via `server/config.py:Settings`)

| Variable | Child | Default | Notes |
|---|---|---|---|
| `CCSWITCH_ALLOWED_ORIGINS` | G11 (consumed by G10, G12) | empty (loopback origins always added) | `scheme://host[:port]`, comma-separated. Startup logs the effective list. |
| `CCSWITCH_EXPOSURE` | G12 | `tailnet` | `loopback\|tailnet\|lan\|public`. Replaces the `app.py:184-209` guard. |
| `CCSWITCH_WEBAUTHN_ENABLED` | G12 | `false` | Replaces bare `WEBAUTHN_ENABLED`. Required `true` in `lan`/`public`. |
| `CCSWITCH_SESSION_IDLE_S` | G12 | `28800` | Idle timeout for passkey sessions. |
| `CCSWITCH_ALLOW_UNAUTHENTICATED` | G12 | — | **Deleted** (recommended) or re-scoped to display — P2. |
| `CCSWITCH_DEV` | G12 | unset | Relaxation limited to `loopback`/`tailnet` tiers. |

### Schema

| Table / column | Child | Change |
|---|---|---|
| `app_sessions.scope` | G10 | new, `String`, **nullable** (NULL ⇒ `"full"`), auto-reconciled |
| `app_sessions.token_id` | G10 | new, nullable FK-by-convention to `assistant_tokens.id` |
| `app_sessions.auth_method` | G12 | new, nullable (`tailscale\|passkey\|display`), NULL ⇒ `tailscale` |
| `app_sessions.last_seen_at` | G12 | new, nullable `DateTime`, naive UTC via `db_utc_now()` |
| `assistant_tokens.scope` | G10 | value set gains `"display"`; column unchanged. `label` (nullable) added for display names |
| `webauthn_credentials` | G12 | **new model**: `id`, `identity`, `credential_id` (bytes, unique), `public_key` (bytes), `sign_count`, `transports`, `label`, `created_at`, `last_used_at`, `revoked_at`. Public keys only — no secret at rest. Postgres suite must be run (new model). |

Migrations: one per child, chained off the newest revision at implementation time.

### HTTP surface

| Route | Child | Scope allowed | Change |
|---|---|---|---|
| every non-`GET`/`HEAD`/`OPTIONS` `/api/*` | G11 | — | ambient callers require allowed `Origin` (403 `ORIGIN_FORBIDDEN`) |
| every response | G11 | — | four security headers (FR-G11.5) |
| `POST /api/kiosk/session` | G10 | unauthenticated + token body | **new**: exchange display token → display cookie, 303 |
| `POST /api/assistant/tokens` (or `/api/kiosk/tokens` alias) | G10 | full | accepts `scope: "display"`, `label`, `expiresInDays` |
| `GET /api/assistant/tokens`, `DELETE …/{id}` | G10 | full | list/revoke includes display tokens |
| `GET /api/auth/me` | G10, G12 | all | returns `{identity, scope, authMode}`; display identity is `display:<label>` |
| `GET /api/pipeline/snapshot`, `GET /api/pipeline/events`, `GET /api/config` | G10 | full, display | display projection strips identifiers |
| `POST /api/auth/webauthn/register/{begin,complete}` | G12 | full (tailnet-authenticated for first credential) | **new** |
| `POST /api/auth/webauthn/login/{begin,complete}` | G12 | unauthenticated | **new**; rate-limited |
| `GET /api/auth/credentials`, `DELETE /api/auth/credentials/{id}` | G12 | full | **new** |
| `POST /api/auth/logout` | — | all | unchanged; also ends display sessions |

### WebSocket surface

| Route | Change |
|---|---|
| `/ws`, `/ws/stt`, `/ws/vnc` | G11: ambient upgrades require allowed `Origin` (1008). G10: display scope refused (1008). Bearer `ccas2:` `act` path unchanged. |
| daemon `/events`, `/vnc` | **unchanged** — transport-gated, out of scope |

### Internal contracts introduced (each replaces N ad-hoc sites with one)

- **G11** — `browser_origin_allowed(request_or_ws, *, ambient: bool) -> bool` beside
  `verify_identity`; a headers middleware in `server/app.py`.
- **G10** — `request.state.scope` / `websocket.state.scope`; a `DISPLAY_ROUTE_ALLOW` table in
  `server/assistant/policy.py` consulted by the existing `route_allowed`; a display projection
  function in `server/api/pipeline.py`.
- **G12** — `server/auth/exposure.py:validate_exposure(settings) -> None` (raises); a rewritten
  `server/auth/webauthn.py` over `py_webauthn`; `auth_method` on `AppSession`.

The Plan stage must grep for an existing equivalent before creating any of these and delete what
each replaces.

---

## 5. Cross-cutting constraints

1. **Single source of truth (VI).** Allowed origins, loopback-ness, peer trust, route allowlists,
   token validation and rate limiting each already have one home; every FR above names it. A
   second implementation of any of them is a spec violation.
2. **Purge in the same PR (VI).** The `app.py:184-209` guard, the `webauthn.py` stubs, the bare
   `WEBAUTHN_ENABLED` read, the hardcoded `authMode`, and (per P2) the LAN-open branch.
3. **Verify against reality (III).** Each child ends with a live smoke on the tailnet edge
   (AC-X.7). Mocked `Origin` handling is not evidence that nginx forwards the header; the smoke
   proves it.
4. **Coverage (V).** ≥85% on `server/auth/`, `server/assistant/policy.py`, and the new routes,
   with negative and abuse cases enumerated in §3.
5. **Vetted library (VII).** `py_webauthn` for every WebAuthn ceremony; no hand-rolled CBOR/COSE.
6. **No prompts in tests (#1409).** Mutation-boundary tests never send a `prompt` frame or call a
   `/prompt` route with a live driver.
7. **Restart discipline.** Every G11/G12 change is a daemon-adjacent edge change; deploy through the
   deploy controller, and the nginx HSTS edit is applied when the Sessions page shows nothing
   mid-turn (`deploy/README.md` § restarting).
8. **Check for in-flight duplicates by file** before each child starts
   (`server/auth/*`, `server/bridge/ws.py`, `web/src/shell/kiosk.ts`). At spec time the only open
   PRs touching SDLC docs are #1578 (E1) and #1579 (E2); `origin/fix-auth-1131-trusted-proxy` is an
   unmerged, superseded branch (#1145 landed the same fix) and should be closed, not rebased.
9. **Branch off `origin/master`**, never local `master`.

---

## 6. Non-goals

- **A full Content-Security-Policy** (script/style/connect sources). Only `frame-ancestors` ships
  here; a report-only CSP rollout is a follow-on ([§8 Q5](#q5-full-content-security-policy-now-or-follow-on)).
- **CORS.** No cross-origin *consumer* of the API is being enabled. G11 rejects foreign origins;
  it does not add `Access-Control-*` headers.
- **Roles beyond `full | display`.** No per-identity RBAC, no "viewer of sessions" scope, no
  per-account permissions. `tailnet-identity-auth.md`'s non-goal ("any allowlisted identity has full
  access") remains true for full scope.
- **A per-frame allowlist on `/ws`.** Display scope is refused the socket outright. If a future
  kiosk page needs live session events, that is a new decision ([§8 Q1](#q1-should-display-scope-ever-get-a-read-only-socket)).
- **Kiosk pages beyond `pipeline`.** The allowlist is seeded for the one kiosk-safe section.
- **Daemon IPC hardening.** `/events` and `/vnc` on the daemon are transport-gated and unchanged.
- **Passkey recovery channels** (email, SMS, recovery codes). Recovery is the tailnet path.
- **Multi-tenant / multi-owner identity.** One allowlist, one operator population.
- **Public exposure itself.** This epic makes the `public` tier *possible and fail-closed*; it does
  not turn it on, open a funnel, or perform the "explicit, documented hardening" review Principle
  IV requires before doing so. Internet exposure was not established by the audit and is not
  established here.
- **Amending the constitution.** Out of this epic's authority; P2 asks the owner.
- **Estimate refinement.** Children carry 24/24/32 h ±50%; the Plan stage re-estimates.

---

## 7. Policy concerns — flagged for the human gate

Not resolved by this spec. Each needs a decision at the Design gate.

### P1. Governance — the Design stage ran without its declared upstream input

**Severity: high (process).** Same gap as E1 §0/P6 and E2 PC-5; already filed via ViberPit
feedback. Recorded here so this artifact's provenance is honest: the intent of record is the epic
and gap register, not an intake artifact.

### P2. Governance — the anonymous LAN branch contradicts Principle IV

**Severity: high.** `CCSWITCH_ALLOW_UNAUTHENTICATED` mints an unidentified caller a full-scope
identity; Principle IV says no deployment is ever reachable by an unidentified caller. #1410 says
"reconcile … with owner-approved governance; do not silently amend the constitution." The
constitution file is managed-policy write-protected in this environment.

*For the gate, choose one:*
**(a) Remove it** (recommended, FR-G12.3): LAN devices without Tailscale use a passkey. No
amendment needed; the only cost is that the LAN-open convenience disappears.
**(b) Keep it at display scope only:** anonymous LAN callers get the kiosk principal, never full.
Still "reachable by an unidentified caller", so it needs a constitution amendment PR — a MINOR
bump with explicit acknowledgement — merged **before** the code lands.
**(c) Keep it as is:** requires the same amendment and leaves a full-privilege anonymous path; the
spec does not recommend this.

### P3. Security — a credential in a URL

**Severity: medium.** FR-G10.3 carries the display token in a URL once. URLs land in browser
history, proxy logs, screenshots and `Referer` headers. Mitigations in the spec: the exchange
strips it via 303, `Referrer-Policy: same-origin` (FR-G11.5), the token is single-purpose and
revocable, and audit redacts it. Residual risk: nginx access logs on the host record the query
string. *For the gate:* accept with a runbook line to disable query-string logging on the vhost, or
require a one-time exchange (token invalid after first use, re-issue on reboot) — stronger, but a
wall display that reboots needs an operator each time.

### P4. Data handling — what an unattended display shows

**Severity: medium.** Even a read-only pipeline wall shows repository names, PR titles, branch
names and session names to anyone in the room. FR-G10.7 strips identities, goals and hostnames.
*For the gate:* ratify that list or extend it (e.g. hide PR titles, show counts only). This is a
product decision the audit's "minimize account/session details" leaves open — [§8 Q3](#q3-what-does-a-wall-display-get-to-show).

### P5. Security — passkey recovery is the tailnet path only

**Severity: medium.** FR-G12.11 makes "sign in over Tailscale, revoke, re-enrol" the only
recovery. For a `public` deployment where the operator has also lost tailnet access, that is a
lockout (mitigable by SSH + a CLI revoke, which this spec does **not** add). *For the gate:*
accept for this single-operator deployment, or add a CLI recovery command to G12's scope.

### P6. Security — origin enforcement can lock out legitimate flows

**Severity: medium.** Turning on FR-G11.3 with a wrong `CCSWITCH_ALLOWED_ORIGINS` makes every
browser mutation 403 and every socket close — a self-inflicted outage on the operator's only
console. The spec deliberately has **no** log-only mode (VI: no half-dead path). *For the gate:*
confirm enforce-on-land with the startup log + AC-X.7 smoke as the safety net, or require a
bounded `CCSWITCH_ORIGIN_ENFORCE=log` mode with a removal issue.

### P7. Security — display scope is a new trust level and a new attack surface

**Severity: low-medium.** A leaked display cookie grants read access to pipeline state for up to
the token's lifetime without any device identity (FR-G10.4). That is intended and bounded, but it
is the first credential in the system that is valid from anywhere. *For the gate:* ratify default
30-day / max 365-day expiry, and whether display sessions should additionally be pinned to the
first peer address seen (breaks displays on DHCP; stronger).

### P8. Data handling — evidence sanitization

**Severity: medium.** As E1 P2: before/after evidence must be seeded fixtures, never live sessions.
Passkey evidence additionally must not include real credential ids or the RP id of the production
host beyond what `deploy/README.md` already publishes.

### P9. Sibling findings outside this epic's scope

**Severity: low-medium, informational.** Found while verifying; not silently absorbed:
- A `ccas2:` **`act`** token also opens `/ws/stt` and `/ws/vnc` (`require_ws_identity` is shared
  with no per-socket policy). Dictation and the VNC relay to a session desktop are a larger grant
  than "act on sessions". Recommend a tracking issue; G10's per-scope WS refusal is the natural
  place to add a per-socket rule later.
- The deploy health probe treats any `< 500` status as healthy (`server/deploy/steps.py:605-606`),
  so a 401 from a mis-set allowlist passes preflight. Unrelated to E3 but relevant once G11/G12
  can 403/401 more requests.
- `origin/fix-auth-1131-trusted-proxy` is unmerged and superseded by #1145; should be closed.

### P10. Ops — ingress changes on a live host

**Severity: low.** HSTS and the query-string-logging line are edits to `deploy/*.nginx` applied by
hand on dobby (the deploy controller does not manage nginx). They need an nginx reload, which does
not touch the daemon, but the runbook must say so.

### P11. ToS / vendor

**None.** `py_webauthn` (Duo Labs, BSD-3) is the only new runtime dependency and is already
declared. No agent binary, credential authority, or vendor surface is touched; constitution I and
II are not engaged.

---

## 8. Open questions for the gate

#### Q1. Should display scope ever get a read-only socket?
FR-G10.6 refuses displays every WebSocket. A future "sessions wall" kiosk page would need
`attach`-only frames, i.e. a per-frame allowlist on `/ws` — the thing `require_ws_identity`'s
docstring says does not exist. **Recommendation: no, not in this epic**; SSE (`/api/pipeline/events`)
is the display transport, and a sessions wall gets its own SSE feed if ever needed.

#### Q2. Should display scope require an ambient identity as well?
FR-G10.4 admits a display on the token alone. Requiring *also* a tailnet identity would confine
displays to tailnet nodes and exclude a plain LAN screen — but would make a leaked cookie useless
off-tailnet (P7). **Recommendation: token-only**, with expiry as the bound; revisit if a display is
ever placed outside the tailnet.

#### Q3. What does a wall display get to show?
FR-G10.7 strips identities, goals and hostnames and keeps repo/PR/branch/session names.
**Recommendation: ratify as stated**; the pipeline wall is only useful with PR titles.

#### Q4. Session lifetime for passkey sessions
24 h absolute (existing) + 8 h idle (new). A `public` deployment might want shorter; a tailnet one
longer. **Recommendation: keep both as env-tunable with these defaults**, and do not add a
"remember me".

#### Q5. Full Content-Security-Policy now, or follow-on?
Vite output generally works under `script-src 'self'` but Tailwind/shadcn inline styles and any
`data:` fonts need verification. **Recommendation: follow-on issue** with a report-only phase;
`frame-ancestors 'none'` ships now because it has no compatibility risk.

#### Q6. ASVS target level
#1410 asks to "record ASVS applicability." **Recommendation: ASVS 4.0 Level 2** for `lan`/`public`
tiers (V2.2, V2.8, V3.2–3.5, V13.2, V14.4), Level 1 acceptance for `tailnet`. The PR records the
mapping; a gap that is not closed is listed, not hidden.

#### Q7. Token table name
FR-G10.2 puts display tokens in `assistant_tokens`, which is the right mechanism under the wrong
name. **Recommendation: keep the table and add the scope value now**; a rename to `api_tokens` is
a separate, mechanical PR if the owner wants it, and must not be bundled with a security change.

#### Q8. Where does the `/kiosk` route live?
Either `/kiosk?token=…` (new client route that performs the exchange) or the existing
`/pipeline?kiosk=1&token=…` (exchange as a side effect of `readKioskConfig`). **Recommendation:
`/kiosk`** — the exchange is an auth action and should not hide inside a layout parser that the
codebase already insists must run exactly once.

---

## 9. Traceability

| Child | Issue | Requirements | Acceptance | Primary source of truth to extend | Blocked by |
|---|---|---|---|---|---|
| G11 | [#1409](https://github.com/steiner385/viberpit/issues/1409) | FR-G11.1–7 | AC-G11.1–8 | `verify_identity` / `require_ws_identity` / `IdentityMiddleware`; `Settings` | — |
| G10 | [#1408](https://github.com/steiner385/viberpit/issues/1408) | FR-G10.1–9 | AC-G10.1–10 | `AssistantToken` + `_validate_user_token`; `route_allowed`; `AppSession`; `InteractionContext` | G11 |
| G12 | [#1410](https://github.com/steiner385/viberpit/issues/1410) | FR-G12.1–13 | AC-G12.1–11 | `Settings`; `host_is_loopback`; `AppSession` cookie path; `server/api/ratelimit.py`; `py_webauthn` | shares FR-G11.1 with G11 |
