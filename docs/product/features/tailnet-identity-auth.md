---
slug: tailnet-identity-auth
title: Tailnet Identity Auth
state: live
links:
  code:
    - server/auth/identity.py
    - server/auth/middleware.py
    - server/auth/webauthn.py
    - server/api/auth.py
    - server/mcp_auth.py
    - server/audit.py
  prs: [632, 673, 837, 830, 877, 232, 811, 648]
---

# Tailnet Identity Auth

**Problem** — ccswitch runs live `claude` sessions across several Max/Copilot accounts and exposes
them to a browser over the network. Anyone who can reach the server can drive those sessions and
read their transcripts, which contain secrets and account credentials. The v1 deployment is a
personal, Tailscale-fronted host, so the auth layer must lean on the tailnet's verified identity
without shipping a password system, while still being safe to open to a LAN or (later) the public
internet. This is constitution Principle IV (security-first, exposure-conditional auth) made
concrete.

**Target users** — the operator running their own ccswitch instance behind `tailscale serve`, plus
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

**Non-goals** — passwords or a bespoke account system (identity is delegated to Tailscale in v1); a
passkey/WebAuthn login path (scaffolded but disabled — see Notes); authorization/RBAC beyond the
single allowlist (any allowlisted identity has full access); terminating TLS in-process (HTTPS is
fronted by `tailscale serve` / an nginx tailnet vhost, PR #811).

**Notes** —
- **Identity resolution** (`server/auth/identity.py`): reads `Tailscale-User-Login` (or the
  `X-Tailscale-User-Login` alternate), falling back to a best-effort `tailscale whois` on the peer
  IP. If `Settings.tailnet_allowlist` is non-empty the identity must be on it or the request is
  denied; an empty allowlist (dev default) admits any identity.
- **LAN-open mode** (PR #632): opt-in `CCSWITCH_ALLOW_UNAUTHENTICATED` admits unidentified callers
  as a synthetic `lan@local` guest — deliberately the "no auth" branch, recognizable in logs and
  session authorship. Off by default. A `SECURITY 0.0.0.0` warning is gated to once per process
  (PR #673).
- **Session cookie** (`server/auth/middleware.py`): `IdentityMiddleware` get-or-creates a
  server-side `AppSession` and issues an `itsdangerous`-signed cookie (24h absolute expiry). The
  signing secret comes from `CCSWITCH_SESSION_SECRET` → `MASTER_KEY` → envelope key, with a warned
  ephemeral per-process fallback (cookies simply reset on restart — identity is re-verified from the
  header every request, so the cookie is bookkeeping only). Cookie-less callers reuse the identity's
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
- **Passkey / WebAuthn path — deferred.** `server/auth/webauthn.py` is a scaffold: every function
  raises `NotImplementedError` and endpoints are 404 unless `WEBAUTHN_ENABLED=true`. It is intended
  for the public-hosting phase (P4). Whether it ships, and its exact UX, is `(unknown — needs owner
  input)`.
- HTTPS/trusted-proxy hardening for tailnet fronting is in flight in open PR #811 (nginx vhost +
  tailscale cert + `X-Real-IP` trusted-proxy fix), building on PR #648 (loopback bind + documented
  `tailscale serve`).

**Evidence** —
- `server/auth/identity.py` — Tailscale header/`whois` identity resolution + allowlist + LAN-open guest.
- `server/auth/middleware.py` — `IdentityMiddleware`, signed cookie, `AppSession` reuse, janitor prune.
- `server/auth/webauthn.py` — disabled passkey scaffold (`WEBAUTHN_ENABLED`, `NotImplementedError`).
- `server/api/auth.py` — `GET /api/auth/me`, `POST /api/auth/logout`.
- `server/mcp_auth.py` — `McpAuthManager` paste-back MCP OAuth.
- `server/audit.py` — redaction patterns + `record_audit`.
- PRs: #632 (LAN-open), #673 (0.0.0.0 warning gate), #837 (app_sessions index + prune), #830 (session
  reuse + prune), #877 (headless MCP OAuth), #232 (no MCP prompt in permissive setup), #648 (loopback +
  tailscale serve), #811 (open — HTTPS via tailnet). Related: #144 (redaction ReDoS fix).
