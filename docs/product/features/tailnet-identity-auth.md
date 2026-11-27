---
slug: tailnet-identity-auth
parent: security-and-isolation
title: Tailnet Identity Auth
state: live
links:
  code:
    - server/auth/identity.py
    - server/auth/middleware.py
    - server/auth/webauthn.py
    - server/auth/exposure.py
    - server/api/auth.py
    - server/mcp_auth.py
    - server/audit.py
  prs: [632, 673, 837, 830, 877, 232, 811, 648]
---

# Tailnet Identity Auth

Restrict application access to verified identities and keep authentication usable from remote devices.

**Problem** — ViberPit runs live `claude` sessions across several Max/Copilot accounts and exposes
them to a browser over the network. Anyone who can reach the server can drive those sessions and
read their transcripts, which contain secrets and account credentials. The v1 deployment is a
personal, Tailscale-fronted host, so the auth layer must lean on the tailnet's verified identity
without shipping a password system, while still being safe to open to a LAN or (later) the public
internet. This is constitution Principle IV (security-first, exposure-conditional auth) made
concrete.

**Target users** — the operator running their own ViberPit instance behind `tailscale serve`, plus
any tailnet peers on the allowlist who are permitted to drive sessions from a phone or laptop.

**Desired outcome** — every HTTP route and WebSocket upgrade is gated on a verified identity;
unidentified or non-allowlisted callers get a clean 401 (HTTP) or 1008 close (WS); the identity is
carried in a signed, HttpOnly, `SameSite=strict`, `Secure` cookie backed by a server-side session
row; nothing that reaches a log sink or audit row leaks a token; and MCP servers can be
re-authorized without a desktop browser.

**Success metrics** — 401/1008 on any request lacking a verified allowlisted identity; zero
hardcoded signing secrets (an ephemeral per-process secret is the last-resort fallback, warned
once); credential-shaped strings never appear in logs, audit rows, or forwarded client logs
(redaction is applied at record, structured-field, and audit-write layers); `app_sessions` table
stays small under sustained cookie-less polling (session reuse + janitor prune).

**Non-goals** — passwords or a bespoke account system (identity is delegated to Tailscale, or to a
passkey bound to the same identity — see Notes); an email/SMS/recovery-code channel (recovery is the
tailnet path); authorization/RBAC beyond the
single allowlist (any allowlisted identity has full access); terminating TLS in-process (HTTPS is
fronted by `tailscale serve` / an nginx tailnet vhost, PR #811).

**Notes** —
- **No per-session ownership — owner ruling, 2026-11-27 (#3533).** The tailnet allowlist is the
  trust boundary: every allowlisted identity may list, read, drive and — via `/ws/vnc` — view the
  display of every session. Sessions deliberately have no owner model (`sessions.owner_identity`
  exists only to key assistant threads), and `/ws/vnc` is not gated more tightly than the session
  APIs that already expose the same content. Do not add a one-off ownership check to a single
  surface; if the allowlist ever holds mutually untrusted users, the decision to revisit is a
  full owner model enforced on every session route (rejected for now as out of proportion for a
  single-operator deployment).
- **Identity resolution** (`server/auth/identity.py`): reads `Tailscale-User-Login` (or the
  `X-Tailscale-User-Login` alternate), falling back to a best-effort `tailscale whois` on the peer
  IP. If `Settings.tailnet_allowlist` is non-empty the identity must be on it or the request is
  denied; an empty allowlist (dev default) admits any identity.
- **Exposure tiers** (`server/auth/exposure.py`, #1410): `VIBERPIT_EXPOSURE` is one of `loopback`,
  `tailnet` (default), `lan` or `public`, validated fail-closed when the edge starts. A broken rule is
  a `RuntimeError` naming the tier, the rule and the fix:

  | Tier | Bind (`VIBERPIT_HOST`) | Requires |
  |---|---|---|
  | `loopback` | loopback | — |
  | `tailnet` | loopback or a tailnet IP | `VIBERPIT_TAILNET_ALLOWLIST`; `VIBERPIT_PUBLIC_ORIGIN` or `VIBERPIT_ALLOWED_ORIGINS` |
  | `lan` | a private address | tailnet's, plus `VIBERPIT_WEBAUTHN_ENABLED=true` with ≥1 registered passkey, `VIBERPIT_SESSION_SECRET` or `VIBERPIT_MASTER_KEY`, and no `VIBERPIT_DEV_INSECURE_COOKIE` |
  | `public` | any | lan's, plus `VIBERPIT_SESSION_SECRET` itself, an https `VIBERPIT_PUBLIC_ORIGIN` and `VIBERPIT_INGRESS_HSTS=true` (the operator attests the ingress sends HSTS) |

  `VIBERPIT_DEV=1` turns `loopback`/`tailnet` refusals into one warning; it never relaxes `lan` or
  `public`. This replaced the old "empty allowlist on a non-loopback bind" guard in `server/app.py`.
- **No anonymous path** (#1410): LAN-open mode (`VIBERPIT_ALLOW_UNAUTHENTICATED`, PR #632) is
  deleted. It admitted unidentified callers as a full-scope `lan@local` guest, which constitution IV
  forbids. An unidentified caller now gets 401 in every tier, and a device without Tailscale signs in
  with a passkey. `lan@local` survives only as `identity.LEGACY_LAN_IDENTITY`, so rows it already
  authored (session owners, hold actors) still read as agent-class "nobody in particular".
- **`authMode`**: `GET /api/auth/me` reports how the request actually authenticated:
  `tailscale | passkey | display | bearer` (`request.state.auth_method`, set by the middleware).
- **Session cookie** (`server/auth/middleware.py`): `IdentityMiddleware` get-or-creates a
  server-side `AppSession` and issues an `itsdangerous`-signed cookie (24h absolute expiry). The
  signing secret comes from `VIBERPIT_SESSION_SECRET` → `MASTER_KEY` → envelope key, with a warned
  ephemeral per-process fallback (cookies simply reset on restart — identity is re-verified from the
  header every request, so for a tailnet session the cookie is bookkeeping only). Cookie-less callers reuse the identity's
  newest live session instead of minting a row per request (PR #830 — prod was minting ~45k rows/day
  for one identity). `/healthz` is exempt so the deploy health-check can probe unauthenticated.
- **Session hygiene**: `prune_app_sessions` janitor reclaims rows expired/revoked past a 7-day grace
  (PR #830); PR #837 added a composite partial index + janitor prune. `POST /api/auth/logout`
  revokes the server-side row and clears the cookie.
- **MCP auth** (`server/mcp_auth.py`): a one-at-a-time state machine drives `claude mcp login` under
  a PTY, surfaces the authorize URL to the UI, accepts the pasted redirect URL, then merges the new
  `mcpOAuth` into the shared store and clears every account's needs-auth cache (headless paste-back
  OAuth, PR #877). PR #232 lets the most-permissive setup skip MCP-tool prompts.
- **Redaction** (`server/audit.py`): `RedactingFilter` + `record_audit`/`redact_event` strip
  `sk-ant-*`, `ccenc1:*`, padded base64 secrets, and signed-cookie shapes from logs, structured
  fields, audit rows, and outbound WS events. The base64 pattern is anchored with a negative
  lookbehind to stay linear-time (PR #144 — an unanchored pattern ReDoS-hung the event loop on a
  ~200KB base64 image in a transcript).
- **Passkeys (WebAuthn)** (`server/auth/webauthn.py`, `server/api/auth.py`, #1410): when
  `VIBERPIT_WEBAUTHN_ENABLED=true`, py_webauthn ceremonies sit behind
  `POST /api/auth/webauthn/{register,login}/{begin,complete}`, `GET /api/auth/credentials` and
  `DELETE /api/auth/credentials/{id}`. All of them 404 while the flag is off. The relying-party id is
  the host of `VIBERPIT_PUBLIC_ORIGIN` (else the first `VIBERPIT_ALLOWED_ORIGINS` entry), and the
  expected origins are the Origin allowlist's own list. Challenges are single-use and expire after
  5 minutes, and user verification is required.
  - *Enrolment*: only a full-scope caller already signed in as that identity (over the tailnet, or
    with one of its passkeys) and attested as a person (`identity.human_sender`) may enrol. The
    passkey is bound to that same identity string, so `VIBERPIT_TAILNET_ALLOWLIST` governs both paths.
  - *Login*: login/begin+complete need no identity (they are exempt from the identity middleware,
    not from the Origin guard). They are rate-limited per peer and per claimed identity through
    `server/api/ratelimit.py:passkey_login_limiter` (429 + `Retry-After`), and every failure writes
    an `auth.passkey.fail` audit row. A sign-count regression (a cloned authenticator) is refused
    and audited.
  - *Session*: a passkey login mints an `AppSession` with `auth_method="passkey"`, and its signed
    cookie is the authenticator. It has a 24 h absolute expiry and an idle timeout
    (`VIBERPIT_SESSION_IDLE_S`, default 8 h, touched per request). The ambient path never adopts a
    passkey session row. Turning the flag off ends every passkey session. A passkey caller counts as
    a person for human-only gates.
  - *Revocation and recovery*: deleting a credential revokes it and every session it minted.
    Recovery from a lost passkey is the tailnet path: sign in over Tailscale, revoke, re-enrol.
    There is no other recovery channel.
  - *Client*: `/login` drives `navigator.credentials.get`. An unauthenticated 401 carries
    `error.login: "passkey"` when passkeys are on, which is what sends the SPA there.
    Settings › Security lists, enrols and revokes passkeys.
- HTTPS/trusted-proxy hardening for tailnet fronting is in flight in open PR #811 (nginx vhost +
  tailscale cert + `X-Real-IP` trusted-proxy fix), building on PR #648 (loopback bind + documented
  `tailscale serve`).

**Evidence** —
- `server/auth/identity.py` — Tailscale header/`whois` identity resolution + the one allowlist rule.
- `server/auth/exposure.py` — the exposure tier validator.
- `server/auth/middleware.py` — `IdentityMiddleware`, signed cookie, `AppSession` reuse, janitor prune.
- `server/auth/webauthn.py` — passkey ceremonies, credentials, passkey sessions (#1410).
- `server/api/auth.py` — `GET /api/auth/me`, `POST /api/auth/logout`.
- `server/mcp_auth.py` — `McpAuthManager` paste-back MCP OAuth.
- `server/audit.py` — redaction patterns + `record_audit`.
- PRs: #632 (LAN-open, removed in #1410), #673 (0.0.0.0 warning gate), #837 (app_sessions index + prune), #830 (session
  reuse + prune), #877 (headless MCP OAuth), #232 (no MCP prompt in permissive setup), #648 (loopback +
  tailscale serve), #811 (open — HTTPS via tailnet). Related: #144 (redaction ReDoS fix).
