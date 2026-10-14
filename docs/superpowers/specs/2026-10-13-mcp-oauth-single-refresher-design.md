# MCP OAuth: one refresher, truthful status, `/mcp` in a session — design

**Date:** 2026-10-13 · **Status:** implemented · Follows `2026-10-09-mcp-live-status-design.md` (#2872) and #2698.

## Incident

On 2026-10-13, every `aws-oauth` call from every account returned `TOKEN_EXPIRED`. The session's MCP readout
said "connected", and `/mcp` in a session only printed the CLI's one-line summary ("Use /mcp in the terminal
for details"). Every account dir's `.credentials.json` held the same `aws-oauth` bundle: the same refresh token,
expired since 2026-10-05 14:24, and dead at the provider. Railway died the same way on 2026-10-02.

## Root cause

#2698 made `store/mcp.shared.json` the cross-account copy of every MCP OAuth bundle. It fixed one bug (a
discovery-only snapshot could revoke a shared token) but left three that together destroy any grant whose
provider rotates refresh tokens. AWS sign-in (public DCR client, `token_endpoint_auth_methods: none`) rotates
them, and a token's lifetime there is one hour.

1. **N independent refreshers of one grant.** Every account dir holds the same refresh token, and CLI 2.1.293
   refreshes proactively when a token has five minutes or less left (verified in the binary: "Token expires in
   …s, attempting proactive refresh"). Its refresh lock and "Another process already refreshed tokens" re-read
   are scoped to one config dir. Every account shares the same `expiresAt`, so their refresh windows coincide.
   The first CLI rotates the token; the next one presents the consumed token and gets `invalid_grant`. A
   provider with reuse detection also revokes the whole family.
2. **`compose()` rolls back rotations.** Session start wrote the shared store's `mcpOAuth` over the account dir
   wholesale. A bundle the CLI had just rotated in that dir, and that `sync_back` had not yet captured, was
   replaced with the consumed one.
3. **A dead grant is resurrected.** After `invalid_grant`, the CLI clears the bundle (`accessToken: ""`, no
   refresh token). The bundle policy says an empty snapshot cannot revoke a shared token, so the next compose
   restored the dead bundle. The CLI then retried it, failed, and cleared it, on every session start. Because
   the AWS server accepts `initialize` without auth, the CLI kept reporting `connected` throughout.

## Decision: ViberPit is the only MCP refresher; account dirs hold no MCP refresh token

The brief offered two shapes: sync one bundle and stop per-dir refreshing, or have each dir sign in on its own.
We chose the first, for three reasons:

- **It is the pattern ViberPit already uses for every other rotating credential.** Claude tokens have exactly
  one refresh authority (`accounts.is_refresh_authority`, "refresh tokens are single-use"), and envelope runners
  hold no refresh token (#2629). OpenAI tokens have a dedicated authority (`viberpit-codex-auth.service`, "the
  only holder of OpenAI refresh tokens"). MCP OAuth was the one credential with N refreshers.
- **Per-dir sign-in contradicts account rotation.** A session lands on whichever account has quota, so the owner
  would meet an unsigned server at random and sign in up to six times per server. #2698 existed to stop that.
- **The CLI makes it safe.** With no refresh token in its file, the CLI never refreshes: it serves the access
  token until expiry, then reports needs-auth ("Token expired without refresh token"). Plaintext credential
  storage is re-read on every token lookup (`readAsync` reads the file), so a live session picks up an access
  token ViberPit rewrites without a reconnect. This was observed live on 2026-10-13, when a re-auth landed in
  `tony_cairnea.com` mid-session and the next `aws-oauth` call succeeded.

### Mechanics

- **Store.** `store/mcp.shared.json` stays the one source of truth and is the only file holding MCP refresh
  tokens. Its writes stay under the existing flock plus the per-key bundle policy (`_mcp_bundle_replaces`).
- **Public view.** An account dir receives `public_view(bundle)`, which is the bundle minus `refreshToken`.
  Client registration (`clientId`, `clientSecret`, `redirectUri`, `discoveryState`) is kept, so a later login
  reuses the DCR client.
- **`sync_cfg_mcp_oauth(cfg_dir, server=None)`** replaces `refresh_cfg_mcp_oauth` and is the one dir↔store
  sync. `compose()`, `after_login`, and the refresher all call it. It has two steps:
  1. **Capture.** It merges any of the dir's bundles that carry a refresh token into the store with the normal
     non-explicit policy, so a newer one wins. That keeps a login made directly in a terminal `claude` under
     that dir.
  2. **Publish.** For each store key, it writes the public view into the dir when the dir copy is missing,
     carries a refresh token, or is replaced under the bundle policy (explicit for *server* after a login). It
     rewrites only the `mcpOAuth` key and keeps the existing stat guard against a concurrent CLI write.
     `compose()` uses the same publish step instead of its wholesale overwrite.
- **The refresher** (`viberpit.mcp_oauth_refresh`) runs as an edge lifespan loop every 60 s, only where
  `accounts.is_refresh_authority()` holds:
  - For each store bundle with a refresh token and an `expiresAt` less than 10 minutes away, it runs the
    `refresh_token` grant against the authorization server's token endpoint. It discovers the endpoint with
    RFC 8414 or OIDC metadata from `discoveryState`, falling back to `<origin>/token`, and sends
    the `resource` the CLI's MCP SDK sends. That is the protected-resource metadata's `resource`
    when one is published, else the server URL minus its fragment. As built, the first version
    sent the raw configured URL. AWS answered `400 INVALID_TARGET` (aws-oauth is configured as
    `…/mcp?oauth=initialize`; its metadata names `…/mcp`), so the grant was never refreshed.
  - It writes the result back compare-and-swap: under the store lock, and only if that key's refresh token is
    unchanged.
  - Then it publishes to every Anthropic account dir. The publish also runs on every tick, so a dir that missed
    an update catches up.
- **Failure classes.**
  - Dead grant: `400`/`401` with (case-insensitively, since AWS answers in upper case) `invalid_grant`, or the CLI's non-standard `invalid_refresh_token`,
    `expired_refresh_token` or `token_expired`. The store bundle is cleared to the CLI's own cleared shape
    (`accessToken: ""`, no refresh token, `expiresAt: 0`), logged at INFO with the server name only.
  - Any other status (other 4xx, 429, 5xx, network): transient. The bundle is untouched and retried next tick.
    An unrecognized `400` (for example `invalid_target`) is never treated as dead, so a protocol mismatch cannot
    wipe a live grant.
- **Logging.** Token values never appear. Logs and tests carry the server name, the key, expiry and presence
  only.

## Truthful status

`sanitize_statuses(raw, credentials=None, now_ms=None)` overlays the account dir's `mcpOAuth`:

- A server with no bundle (`serverName == name`) is not OAuth: no change, and `auth` is `None`.
- Otherwise `auth` is `"oauth"`. If no bundle has a usable token (a non-empty access token that has not expired,
  or has no expiry), the state is `needs-auth`. The error is "Sign-in expired …" when an expired token exists,
  and "Not signed in" when every bundle is empty. A `disabled` server is left alone.
- On a live session, the driver also latches **observed** auth failures. These are MCP tool results whose text
  carries `TOKEN_EXPIRED`, `invalid_token`, `401` or `Unauthorized`; the server is resolved through the tool
  name's `mcp__<normalized server>__` prefix. The latch reports `needs-auth` with "Sign-in rejected by the
  server". It clears on a successful tool result from that server, on `reconnect_mcp_server`, or when the
  server's access token changes in the credentials file. The token is compared by an in-memory hash and never
  logged.

`McpServerState` gains `auth` (wire `auth: "oauth" | null`).

## `/mcp` inside a session

- `mcp` becomes a client command. It is in `CLIENT` and gated on `AgentCapabilities.mcp_status`. Dispatch
  returns `{"render": "mcp", "data": {"servers": [...]}}` from `driver.mcp_status()`, so the CLI's
  terminal-only summary is no longer what the user sees.
- The web renders that result as the session's MCP panel: the same per-server list as the `MCP n/m` readout
  (status, error, tool count, Reconnect), plus **Reauth** on any server with `auth == "oauth"` or status
  `needs-auth`. Reauth opens the existing `McpAuthFlow` for the session's own account, so the login runs in
  that account's `CLAUDE_CONFIG_DIR`. On success, `after_login` publishes the bundle to every account and
  reconnects the server in the live sessions that were failing.

## A browser on another host

The login PTY (`claude mcp login --no-browser`) runs on the edge, which owns the account store. It never runs
on the session's worker. The browser needs only the authorization URL. When the provider redirects to
`http://localhost:<port>/callback` and that page fails to load on the browser's device, the user pastes the
address bar back into the flow, so the flow is independent of which host the session or the browser is on.
`McpAuthFlow`'s copy says this explicitly.

## Testing

- Core: public view; capture + publish; compose no longer rolls back a rotated bundle, never resurrects a
  cleared one, and never writes a refresh token into a dir; the refresher's due-selection, CAS, dead and
  transient classes, endpoint discovery and resource param (fake HTTP); no token in logs.
- Server: the status overlay cases; the driver latch (set, clear on ok result, reconnect, token change); `/mcp`
  catalog + dispatch.
- Web: the `/mcp` render; Reauth visibility for `auth: "oauth"`; the full `npm run test`.
- Live (rocky): re-authenticate `aws-oauth` through `/mcp` in a session, then confirm a fresh
  `tonystein85_gmail.com` session can call `sts:GetCallerIdentity`, and that only the store holds a refresh
  token afterward.
