---
slug: mcp-status
parent: config-library-snapshots
title: MCP Live Status and Reauth
state: live
links:
  specs:
    - docs/superpowers/specs/2026-10-09-mcp-live-status-design.md
    - docs/superpowers/specs/2026-10-09-mcp-repo-scope-design.md
  code:
    - server/mcp_health/probe.py
    - server/mcp_health/store.py
    - server/mcp_health/status.py
    - server/mcp_health/after_login.py
    - server/mcp_health/one_tap.py
    - server/api/mcp_oauth.py
    - src/viberpit/mcp_oauth_client.py
    - server/api/library.py
    - server/api/sessions.py
    - server/sessions/claude_driver.py
    - server/mcp_scope/resolve.py
    - server/mcp_scope/apply.py
    - server/mcp_scope/store.py
    - web/src/features/settings/sections/McpScopeControl.tsx
    - src/viberpit/creds.py
    - web/src/features/settings/sections/McpSection.tsx
    - web/src/features/sessions/McpStatusReadout.tsx
---

# MCP Live Status and Reauth

Show whether each MCP server actually works, per account and per host, and let the operator test it, reconnect it, or re-authenticate it without leaving the page.

**Problem** — Settings › MCP could add, edit and delete servers and run a paste-back OAuth login, but could not say whether a server connected. Needs-auth was inferred from the CLI's per-account cache file, the SDK's `mcp_servers` init data was discarded, and a live session never learned that a login had just succeeded.

**Target users** — operators wiring MCP servers across several accounts and hosts, and anyone in a live session whose MCP tools have gone missing.

**Desired outcome** — one persisted status per (account, host, server), visible in Settings and inside the session, refreshed by live sessions and by an on-demand test that costs no model tokens.

## Settings › MCP

Each server shows one **overall** status, derived on the server (`server/mcp_health/overall.py`, returned as `overall` by `GET /api/library/mcp`), in this order:

1. A counted row is `failed` → **Failed**, with its reason. A configuration problem signing in cannot fix.
2. The shared grant in `store/mcp.shared.json` is never/dead/expired → **Needs auth**. The grant is read at the CLI's exact key (`<serverName>|<hash of type, url, headers>`, `viberpit.mcp_keys`), so a bundle filed under any other key is ignored. Only http/sse servers with an explicit `type` have a knowable key; any other server reads as no grant without a lookup.
3. The grant is live or expiring, but a live session observed the server rejecting the token → **Failed**, "token rejected".
4. Otherwise, the best counted row status. No counted rows → **Not checked yet**.

Rows that count: enabled accounts only, and only rows observed within the last 24 hours; probe rows always; session rows only while their session is not ended or archived. `disabled` renders neutral. Expanding Details still lists every account x host row, with status, error, tool count, and "checked 3m ago" with its source (`session` or `probe`). Uncounted rows are dimmed and marked "not counted".

Plugin servers appear under their CLI name (`plugin:<p>:<s>`); the pill and readout show the short name. Only user-scope and plugin servers are covered; project `.mcp.json` servers are out of scope.

## Test and Test all

**Test** (per server) and **Test all** (section header) call `POST /api/library/mcp/test` with an optional `{account, server}`. The probe opens an SDK client, polls the server list until nothing is `pending`, disconnects, and records rows with `source = probe`. It sends no prompt and spends no tokens.

- The probe runs on every host's daemon against that host's local scratch mirror of the account config (via `account_auth.helper_env`), never the real config dir, so it cannot disturb a concurrent login or live session.
- Budgets: 25 s probe settle per account, 33 s per-host deadline, 35 s for the whole endpoint. Still-pending servers are recorded as `pending`.
- A host that cannot finish reports a skipped reason: `timed out`, `budget exhausted`, or `unknown account`.
- Account names in the request are checked against the known account set; anything else is rejected with 400 `unknown_account`.
- One probe per account at a time; a second caller awaits the first.

## Reauth follow-through

**Fix auth** appears once per server when the grant is never/dead/expired (http/sse only) and opens the paste-back `McpAuthFlow` with no account picker. One sign-in covers every account and runner; the edge runs the login in an account's dir, preferring an enabled one. On success the edge:

1. Syncs only the `mcpOAuth` key of each account's `.credentials.json` (`sync_cfg_mcp_oauth`), with a stat guard against a concurrent CLI write, using the same merge policy as `merge_shared_mcp`. The dir receives the bundle's public view, which has no refresh token.
2. Reconnects that server in every live session whose latest row for it is `needs-auth` or `failed` (bounded fan-out: 4 concurrent, 15 s each).
3. Re-probes the account.

Reconnect failures are logged and show up in the next status poll; they never fail the login response.

## One-tap sign-in

**What the user sees** - when `VIBERPIT_PUBLIC_ORIGIN` is set (`GET /api/config` reports `oneTapEnabled`), a server that needs auth (http/sse, grant never/dead/expired) shows a **Sign in** link in Settings › MCP instead of the paste-back form. It goes to the provider, then back to Settings, which toasts the result. The `mcp_auth_needed` push deep-links straight to `/api/mcp/oauth/start?server=<name>` (to `/settings/mcp` when one-tap is off), so one tap from the notification starts the sign-in.

**How it works** (`src/viberpit/mcp_oauth_client.py`, `server/mcp_health/one_tap.py`, routes in `server/api/mcp_oauth.py`)
- **Discovery** - the protected-resource metadata (RFC 9728) is looked for at the `WWW-Authenticate` `resource_metadata` URL (same origin only), then the path-inserted well-known URL, then the origin root. Its `resource` must sit on both its own origin and the server's. Authorization-server metadata follows (RFC 8414, then OIDC).
- **Client** - ViberPit registers itself by Dynamic Client Registration (RFC 7591) once per authorization server and caches the client in `store/mcp.clients.json`. A cached client is reused only for the same redirect URI (`{VIBERPIT_PUBLIC_ORIGIN}/api/mcp/oauth/callback`).
- **Authorize** - PKCE S256, an RFC 8707 `resource` indicator, and a random `state` that is single-use, expires in 10 minutes and is bound to the tailnet identity that tapped (at most 100 pending). A callback with an unknown, replayed or foreign-identity state is refused and writes nothing.
- **Store** - the code exchange writes the bundle to `store/mcp.shared.json` at the CLI's exact key (`grants.key_for`), then schedules the same `after_login` follow-through as paste-back (account-dir sync, reconnect, re-probe).
- **Audit** - `library_mcp_auth_started` / `library_mcp_auth_completed`, carrying the server name only (never the URL, which holds the state).

**Fallback to paste-back** - the redirect carries `?pasteBack=<server>` and Settings opens the paste-back `McpAuthFlow` for that server when: no public origin is set; the server has no knowable CLI key; discovery fails; the provider offers no dynamic client registration (logged at INFO) or refuses registration; or the token exchange answers `invalid_client` (the cached client is forgotten, so the next tap re-registers).

**Results** - every route answers a 302 to a relative `/settings/mcp?...`; provider text is never reflected, and the UI maps each code to fixed copy.
- `signedIn=<server>` - "Signed in to {server} — every account and runner can use it now."
- `signInError=` `state_invalid` (link expired or already used), `denied` (cancelled at the provider, or no code), `exchange_failed` (the provider rejected the code, or the grant could not be stored), `not_configured` (not a single configured http/sse server); `registration_failed` exists in the set but a refused registration falls back to paste-back instead.

**Security limits** - the authorization server and every endpoint it names must be https; discovery, registration and token requests never follow redirects and cap responses at 1 MiB; the server name is validated before it is echoed anywhere. The routes are reachable only with a human tailnet identity: assistant and internal tokens are not on their allowlist. The origin comes from settings, never from request headers.

**First live use** - the first tap per provider registers ViberPit as an OAuth client with that provider.

**KinDash** - its authorization server offers no dynamic client registration, so one-tap (like the CLI) cannot sign in to it and it stays on its Failed row. Tracked in cairnea/KinDash#20485; ViberPit adds no static-client config.

## Token refresh

`store/mcp.shared.json` is the only file that holds MCP refresh tokens, so no session's CLI refreshes a shared grant. On the refresh authority, the edge refreshes each bundle that expires within 10 minutes. It checks every 60 s, writes the result back compare-and-swap, and publishes the public view to every Anthropic account dir. Session start (`compose`) uses the same sync, so it never rolls back a rotation and never writes a refresh token into a dir. A dead grant (`invalid_grant` and the CLI's equivalents) is cleared to the CLI's signed-out shape and needs a Reauth. Any other failure is retried on the next tick.

**Reconcile** - Each tick, before refreshing, the edge files every grant under its server's current CLI key (`server/mcp_health/grants.py`, `viberpit.mcp_rekey`). Editing a server's URL or renaming it changes the key and would orphan the live grant, so:
- **Keys:** the wanted set is every configured http/sse server with an explicit `type`; its key is the CLI's formula over `type`, `url` and `headers` (`grants.key_for`, which the status readout also uses). A config with `${…}` in its URL or a header value is skipped: the CLI hashes the expanded value.
- **Moves:** a grant moves to a wanted key only when its grant identity (from the protected-resource metadata) equals the server's, both URLs share an origin, and the wanted key already exists in the store. The CLI writes a discovery bundle there when it first connects, so this guards against our key formula drifting from the CLI's; until then nothing moves. An unknown identity never moves. Identities are fetched before the store lock is taken, never under it, and a failed lookup is not retried for 5 minutes.
- **Refresh tokens are never deleted:** a bundle holding a refresh token is only ever moved, in the store and in account dirs alike.
- **Drops:** an empty bundle (no token) is deleted only when it is provably dead: its name is a configured server's (superseded by that server's current key), or it is `plugin:<plugin>:<name>` for a plugin that is not enabled. Any other empty bundle is kept, since a project `.mcp.json` may own it. An access-token-only bundle superseded by a configured server of the same name is deleted only once the server's current key holds a live grant or both identities are known and differ. An unconfigured bundle with a token is kept.
- **Purge:** the dropped keys and moved bundles' old keys are purged from every account dir in the same tick's publish, so capture never offers a stale copy back. Each purged key stays tombstoned for 30 minutes, so a dir that missed a tick still gets it purged. If `claude.shared.json` exists but can't be read, the tick skips reconcile (one WARNING) and purges only tombstoned keys; an absent file just means no user servers.
- **Single writer:** only the refresh-authority host (dobby: the edge, the primary daemon and the CLI) writes the shared store, serialised by its lock; workers never do. A worker's reconcile is a no-op, and its merge and capture leave the store alone. Logs carry keys and counts, never token values.

**Heal on refresh** - when a refresh succeeds for a grant that was failing (`consecutive_failures > 0`, or health state `dead`/`expired`), the edge runs the reauth follow-through (`after_login`) for that server, probing at most once per tick. A routine refresh of a healthy grant never heals.

**Push when a shared sign-in dies** - one push, event class `mcp_auth_needed` (web label "MCP sign-in needed", delivered even with no rules configured), deep link `/settings/mcp`. Title `MCP sign-in needed: {server}`. Body for a dead grant: `{server}'s sign-in was revoked or expired. Sign in once to restore it for every account and runner.` Body for an expired grant that refresh keeps failing: `{server}'s sign-in expired — background refresh is failing: {reason}. Sign in again.` (reason truncated to 160 chars). Dedupe is one push per grant generation: the generation is the grant's last successful refresh (`last_ok_at`, or "never"), recorded in the health row's `alerted_for` as `{state}:{generation}`. A successful refresh or a fresh sign-in starts a new generation. A grant that goes expired and later dead in one generation may push twice, as an escalation. Alerts are decided from the health table every refresher tick, never from that tick's outcomes alone: each tick first marks `dead` any grant that was signed in (has a `last_ok_at`) whose store bundle is now cleared (no refresh token, empty access token) — which catches a death whose one-off `dead` outcome failed to record, and one the CLI cleared itself — then pushes every `dead` or `expired` row whose `alerted_for` is not `{state}:{generation}`. A push whose copy or delivery record fails is therefore retried next tick. A cleared bundle that was never signed in here (no row, or no `last_ok_at`) never alerts.

**Status** - a cleared grant whose health row says `dead` reads "Sign-in revoked or expired"; an expired grant shows the last refresh failure in the same `overall.error` text. A live or expiring bundle always wins over a stale health row.

**Health table** - `mcp_grant_health` (migration `0101`), one row per store key: `server`, `state`, `last_attempt_at`, `last_ok_at`, `last_outcome`, `last_error` (refresh error text, at most 500 chars, never a token), `consecutive_failures`, `alerted_for`, `alerted_at`, `updated_at`. It is durable, so alert dedupe and healing survive restarts.

**Superseded keys are inert** - a key holding a refresh token is skipped (never refreshed) only when it is not any wanted key AND its `serverName` is a configured (wanted) server's — an old key of a configured server. Rotating a refresh token on it would invalidate the grant the CLI actually uses. A refresh-bearing key whose server is unconfigured or unscanned (a repo or local `.mcp.json` server, or one whose CLI key cannot be known) is not superseded and keeps refreshing.

**Kill switch** - `VIBERPIT_MCP_OAUTH_REFRESH_ENABLED` (default on). When off, nothing refreshes MCP grants, and a token stays valid only until it expires. Reconcile runs in the same tick, so the switch disables it too.

## In-session readout

The session's MCP servers are listed inline in the session rail's **Context** card (D-6; it replaced the `MCP n/m` header pill and its popover). On a pane narrower than 64rem the rail's cards move into the **Details** tab. The list renders only when the session reports at least one user MCP server. Each row shows the server's status, error and tool count, with:

- **Reconnect** - `POST /api/sessions/{id}/mcp/{server}/reconnect` (audited as `session.mcp_reconnect`)
- **Reauth** - opens `McpAuthFlow` in place
- **Manage** - links to Settings › MCP

Data loads from `GET /api/sessions/{id}/mcp` until the first live `mcpStatus` session event arrives, then follows those events (`useMcpServers`). The internal ViberPit app server is hidden (filtered by name). The same list backs the `/mcp` command panel.

**Attention dot** - while the rail is out of sight, an amber dot flags anything in it worth a look. The dot appears on the Details tab on a narrow pane, and on the toolbar's rail toggle when the rail is collapsed on a wide pane. Its accessible name gains "(needs attention)", for example `Show details (needs attention)`. It shows when any MCP server is `failed` or `needs-auth`, when background work is running or a wakeup is armed, or when a sub-agent has failed. One predicate, `web/src/features/sessions/rail/railAttention.ts:railNeedsAttention`, decides it for both places. It reads the same live-then-snapshot server list as the Context card.

## Scope

Sub-project 2a. By default every session loads every user-scope and plugin MCP server. Scope lets an operator turn a server off by default, turn it on for named repos only, turn it off for chosen repos, and flip any server inside a running session. A disabled server never connects, never prompts for OAuth and never loads tools.

**Precedence** - for each configured server the first source with a value wins: session override, then repo rule (`Session.repo`, `owner/name`, case-insensitive; scratch sessions have no repo and skip this step), then the server default, then on. One pure function, `server/mcp_scope/resolve.py:effective_disabled`, produces the disabled list.

**Rules** - managed in Settings › MCP (`McpScopeControl`) through the library API:

- `GET /api/library/mcp/scope` - server defaults and repo rules.
- `PUT /api/library/mcp/scope/{server}` - set the server default (`{defaultEnabled}`).
- `PUT /api/library/mcp/scope/{server}/repos/{owner}/{name}` - set a repo rule (`{enabled}`). Validates the server is configured, the repo slug shape, and that the repo is a known repo.
- `DELETE` on the same path removes the rule. It skips the known-repo check so a stale rule for a repo that has gone away stays removable.

Plugin servers are keyed by the plugin manifest name (`plugin:Notion:notion`, not the registry id); the UI's `serverKey` and the server's `server_key` share one test table so a rule is always saved under the key the CLI reports.

**How it reaches the CLI** - at every driver build (spawn, resume, reassign, adopt) the effective list is written to the CLI's `projects[<worktree>].disabledMcpServers` through `configdir.trust_project`. The driver also re-enforces it after start: on its first successful MCP status poll (and after a `/clear` reconnect) it calls `toggle_mcp_server(name, False)` once for each desired-disabled server not already `disabled`. This closes a race where `compose()` rewrites the account config while the CLI is starting.

**Live toggle** - each row of the in-session MCP list gets an on/off switch (`POST /api/sessions/{id}/mcp/{server}/toggle`, `{enabled}`; 400 unknown server, 409 if the CLI refuses, 404 if the session is not live). The choice is saved to `sessions.mcp_overrides` and survives resume; the driver's enforcement set is kept in sync. The DB is authoritative: the CLI also mirrors toggles into `.claude.json`, but that copy is overwritten at the next driver build.

**Repo key** - repo rules key on the session's stored repo (`Session.repo`, set at creation), not the live git remote some UI labels show. Adopted and explicit-cwd sessions have no stored repo and get server defaults only.

**Switch coverage** - the in-session list shows a switch only for user-scope and plugin servers (CLI scope `user` or `dynamic`); `project`, `claudeai` and `local` rows keep status, Reconnect and Reauth but no switch, since the toggle route rejects them.

**Applies to new and resumed sessions** - changing a repo rule or server default does not touch sessions that are already running. Use the live toggle for those.

**Kill switch** - `VIBERPIT_MCP_SCOPE_ENABLED` (default on). When off, no list is written and both the rules API and the toggle route return 404 `{"error": "mcp_scope_disabled"}`; Settings hides its scope controls immediately; the in-session list hides its switches only after the first toggle returns 404 `mcp_scope_disabled`. Post-start enforcement runs from the MCP status poll, so it also requires `VIBERPIT_MCP_STATUS_ENABLED` on; with status off only the pre-launch list protects a session.

**Known limitations**

- Sessions that share a non-git cwd (adopted or explicit-cwd sessions with no worktree) share one `projects[<cwd>]` entry, so two live ones with different overrides last-writer-win on that list. Git-repo sessions each have a unique worktree key.
- A server flipped off just before a kill-switch flip could stay off for a resumed session in the same worktree (the list is not rewritten while the switch is off).
- In the start-up race case a disabled server may briefly connect before post-start enforcement switches it off.
- Codex sessions are not supported (the toggle returns 409).
- Committed `.mcp.json` servers are out of scope here (planned as sub-project 2b).

## Limits and switches

- **Codex** sessions do not report MCP status yet; the readout is absent and the endpoints return an empty list. Planned in sub-project 4 of the MCP management series.
- **Kill switch** `VIBERPIT_MCP_STATUS_ENABLED` (default on) disables status reporting, persistence, the probe and the readout; the API then returns empty status lists.

## Data model

Table `mcp_server_status` (model `McpServerHealth`, migration `0078`), unique on `(account, host, server)`; each row is the latest observation. Columns: `status`, `error` (sanitized), `tool_count`, `scope`, `source` (`session` | `probe`), `session_id`, `observed_at` (`db_utc_now()`). It replaces the old cache-file reader for needs-auth, which was deleted.

Scope adds tables `mcp_server_scope` (`server` PK, `default_enabled`, `updated_at`) and `mcp_repo_rule` (unique `(repo, server)`, `enabled`, `updated_at`), and the nullable column `sessions.mcp_overrides` (JSON `{server_key: bool}`), all in migration `0079`.
