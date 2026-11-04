# MCP auth overhaul — Phase 4: one-tap sign-in — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tapping "MCP sign-in needed" (or Fix auth) opens the provider's sign-in page. When it
finishes, the browser lands back on ViberPit and the server works for every account and runner.
There is no copy-paste. Today's paste-back flow stays as the fallback for providers that refuse
an https redirect.

**Architecture:** ViberPit becomes its own OAuth client, public with PKCE (RFC 7636 + 8707), on
the edge:
- **Discovery** follows the CLI: first `WWW-Authenticate: resource_metadata=…` from an
  unauthenticated request, then RFC 9728 path-insert, then the root well-known.
- **Registration** uses DCR (RFC 7591), cached per authorization server. The `redirect_uri` is
  `{VIBERPIT_PUBLIC_ORIGIN}/api/mcp/oauth/callback`.
- **Start** is `GET /api/mcp/oauth/start?server=<name>`, which 302s to the authorize URL.
- **Callback** is `GET /api/mcp/oauth/callback`. It exchanges the code and writes the bundle in
  the CLI's shape at `grants.key_for(server)`, through the existing explicit-login merge. It then
  runs `after_login(signed_in=True)` and 302s to `/settings/mcp`.

**Spec:** `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md` §5. Phase 3 parked
two false-push edge cases; Task 0 fixes them.

**Precondition:** #3271 (Phase 3) has merged.

**Provider probe (2026-11-04, read-only):**

| Server | PRM location | Auth server | DCR | PKCE | Token auth |
|---|---|---|---|---|---|
| aws-oauth | `…/.well-known/oauth-protected-resource/mcp` | `https://us-east-1.oauth.signin.aws/` | yes (`/v1/register`) | S256 | `none` |
| railway | `…/.well-known/oauth-protected-resource` (root) | `https://backboard.railway.com` | yes (`/oauth/register`); also CIMD | S256 | `none` among others |
| kindash-mcp | root, via `WWW-Authenticate` only (path-insert 404s) | qa.kindash.com | **no** | S256 | — |

Whether AWS and Railway accept an `https://dobby.tail0dd570.ts.net/...` `redirect_uri` can only
be learned by registering. Task 2's first live run records the answer. A refusal flips that
provider to paste-back. **KinDash is blocked:** it offers no DCR, so neither ViberPit nor the
CLI can sign in. See "Open decision".

## Global Constraints

- **New setting `VIBERPIT_PUBLIC_ORIGIN`** (e.g. `https://dobby.tail0dd570.ts.net`). It must use
  the `VIBERPIT_` prefix (brand ratchet). Validate it as `https://host[:port]` with no path.
  When it is unset, one-tap is disabled and every server uses paste-back. Never derive the
  origin from request headers.
- **Security:**
  - `state` is 32 random bytes, url-safe, single-use, with a 10 min TTL. It is bound to the
    tailnet identity that started the flow; the callback rejects a different identity.
  - The PKCE verifier is 64 random url-safe chars; the challenge is S256.
  - Send `resource` exactly as the refresher does (`url_href` of the PRM `resource`). Origin
    binding follows Phase 2's `grant_identity`.
  - Both routes sit behind `current_identity`.
  - The callback never reflects provider `error_description` into HTML or URLs. Only a
    redirect with `?signInError=<code>` is allowed, where the code is from a closed set:
    `state_invalid`, `denied`, `exchange_failed`, `registration_failed`, `not_configured`.
  - Never log codes, verifiers or tokens.
- **Bundle shape** (CLI-compatible; the refresher already refreshes with `bundle.clientId`):
  `{serverName, serverUrl, accessToken, refreshToken?, expiresAt(ms), scope?, clientId, clientSecret?, redirectUri, discoveryState{authorizationServerUrl, resourceMetadataUrl, authServerMetadataUrl?, oauthMetadataFound: true}}`.
  It is written with `creds.merge_shared_mcp({key: bundle}, authenticated_keys={key})` at
  `grants.key_for(server)`.
- **Client registration cache:** `store/mcp.clients.json` holds
  `{issuer: {client_id, client_secret?, redirect_uri, registered_at}}`. It is written only on
  the edge via `creds.write_json_secure` (0600), under its own lock helper modelled on
  `_shared_mcp_locked`. Registration is retried only when `redirect_uri` changed or the
  provider rejects the client (`invalid_client`).
- **Deep link swap.** The push `url=` becomes `/api/mcp/oauth/start?server=<urlquoted name>`
  when one-tap is available for that server, and stays `/settings/mcp` otherwise.

## Review Focus

1. **A callback with a valid code but a state from another tailnet identity, or a replayed
   state:** rejected, and nothing is written. Task 3 covers this with
   `test_callback_rejects_foreign_or_replayed_state`.
2. **A provider that refuses registration or the redirect:** start falls back to
   `/settings/mcp?pasteBack=<name>`, which opens the existing flow. Task 3 covers this with
   `test_start_falls_back_to_paste_back_on_registration_failure`.
3. **`VIBERPIT_PUBLIC_ORIGIN` unset:** no route 500s, Fix auth uses paste-back, and the push
   links to `/settings/mcp`. Tasks 3 and 4 cover this.
4. **A sign-in finishing in the same second as a refresh tick:** no false "revoked" push.
   Task 0 covers this.
5. **A token response without `refresh_token` or `expires_in`:** stored as-is (`expiresAt`
   absent), and the status reads Connected. Task 3 covers this with
   `test_callback_handles_access_only_token`.

---

### Task 0: Close Phase 3's parked false-push edges

- `server/app.py` `_mcp_oauth_tick`: re-read the store (in a thread) **immediately before**
  `promote_cleared`, and compute `cleared_keys` from that fresh read.
- `grant_health.promote_cleared` only promotes rows whose `last_outcome != "signed_in"`, or
  whose `updated_at` is older than 60 s.
- `after_login._mark_signed_in`: stamp only keys whose current store bundle is usable (holds a
  token), which closes the duplicate-short-name stub case.
- Tests: `test_sign_in_during_tick_is_not_promoted_dead` and
  `test_mark_signed_in_skips_tokenless_stub`.
- Commit: `fix(mcp): no false 'revoked' push around a sign-in`.

### Task 1: The `VIBERPIT_PUBLIC_ORIGIN` setting

- Add it in `server/config.py`, with validation, docs in `deploy/README.md`, and the env
  example. Expose `oneTapEnabled: bool` in `GET /api/config`.
- Tests cover validation (an https origin is accepted; paths, http and junk are rejected) and
  the config flag.
- Commit: `feat(mcp): public origin setting for OAuth callbacks`.

### Task 2: `viberpit.mcp_oauth_client`: discovery, registration, authorize URL, code exchange

- Module `src/viberpit/mcp_oauth_client.py`. It reuses `mcp_oauth_refresh`'s `_default_fetch_json`,
  `token_endpoint` and `url_href`, and `mcp_keys.url_origin`.
- Functions:
  - `discover(server_url, *, fetch=…, head=…) -> Discovery`: PRM URL from `WWW-Authenticate`,
    then path-insert, then root. Then the AS metadata. Origin binding as in Phase 2.
  - `ensure_client(discovery, redirect_uri) -> Client`: the cache, then DCR with
    `{"client_name": "ViberPit", "redirect_uris": [...], "grant_types": ["authorization_code","refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}`.
  - `authorize_url(discovery, client, *, state, challenge, scope) -> str`
  - `exchange_code(discovery, client, *, code, verifier, redirect_uri) -> dict`, which returns
    the bundle per Global Constraints.
- Scope comes from the PRM `scopes_supported`, else AS `scopes_supported` ∩ {offline_access, …}.
  Request `offline_access` when the AS supports it. Railway's existing bundle uses
  `"offline_access workspace:member"`.
- Tests use fakes for the three provider shapes in the probe table, including KinDash's
  "no `registration_endpoint`", which raises `RegistrationUnsupported`.
- Commit: `feat(mcp): ViberPit's own MCP OAuth client`.

### Task 3: Start and callback routes, plus the edge flow

- Module `server/mcp_health/one_tap.py`:
  - An in-memory state table: `{state: (identity, server, verifier, created_at)}` with TTL
    pruning.
  - `start(identity, server_name) -> str` (the URL to redirect to).
  - `complete(identity, state, code | error) -> server_name`.
- Routes in `server/api/library.py` (or a new `server/api/mcp_oauth.py` router mounted at
  `/api/mcp/oauth`):
  - `start`: 302 to the provider. On any `OneTapUnavailable`, 302 to
    `/settings/mcp?pasteBack=<name>`.
  - `callback`: 302 to `/settings/mcp?signedIn=<name>` or `?signInError=<code>`.
- After a successful write, `after_login(app.state.sessions, server, accounts, signed_in=True)`
  runs as a background task.
- Audit `library_mcp_auth_started` and `library_mcp_auth_completed` with the server name only.
- Tests: the Review Focus 1, 2, 3 and 5 cases, a happy path that writes the bundle at the exact
  CLI key, and a check that `merge_shared_mcp` is called with `authenticated_keys`.
- Commit: `feat(mcp): one-tap MCP sign-in start and callback`.

### Task 4: Web, and the push deep link

- `McpSection` Fix auth is a link to `/api/mcp/oauth/start?server=` when
  `config.oneTapEnabled`. Otherwise it is the existing `McpAuthFlow`.
- On load, `?pasteBack=<name>` auto-opens that server's `McpAuthFlow`. `?signedIn=<name>` shows
  a success toast. `?signInError=<code>` shows a fixed message per code.
- `server/mcp_health/alerts.py`: when `get_settings().public_origin` is set, the `url=` is the
  start link.
- Tests cover the web query-param handling, the Fix auth link, and the alert deep link with
  and without the origin. Run the full web gate.
- Commit: `feat(web): one-tap MCP sign-in from Settings and the push`.

### Task 5: Live check, docs and the PR

- Docs: in `docs/product/features/mcp-status.md`, cover one-tap, the fallback, and the setting.
  In the spec's Delivery order item 4, link this plan. In `deploy/README.md`, cover the setting.
- **Live check, after merge and deploy, with Tony:** set `VIBERPIT_PUBLIC_ORIGIN`, then tap
  Fix auth for railway and for aws-oauth. Record in the PR whether each provider accepted the
  https redirect. This step registers ViberPit as a client with each provider, and only Tony
  starts it.
- Run the full gate. Open a ready PR that says "Phase 4 of 5", and arm `--squash --auto`.

## Decision (Tony, 2026-11-04): KinDash → option (a), tracked as cairnea/KinDash#20485. Phase 4 adds no static-client config.

### Options considered

KinDash's auth server offers no dynamic client registration, so no MCP client can sign in to it
today, the CLI included. Options:

- (a) Add DCR to KinDash's auth server. This happens in the KinDash repo, and ViberPit needs no
  change.
- (b) Pre-register a "ViberPit" client in KinDash, and have ViberPit accept a static client
  config per server (`oauth: {clientId}` in the server config).
- (c) Leave KinDash unsupported. Its page row stays Failed, with the "no dynamic client
  registration" reason.
