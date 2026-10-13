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

Each server row carries a status pill showing the worst status across its rows, ranked failed > needs-auth > pending > connected. `disabled` renders neutral. A server with no rows reads **Not checked yet**. Expanding a row lists every account x host observation: status, error, tool count, and "checked 3m ago" with its source (`session` or `probe`).

Plugin servers appear under their CLI name (`plugin:<p>:<s>`); the pill and readout show the short name. Only user-scope and plugin servers are covered; project `.mcp.json` servers are out of scope.

## Test and Test all

**Test** (per server) and **Test all** (section header) call `POST /api/library/mcp/test` with an optional `{account, server}`. The probe opens an SDK client, polls the server list until nothing is `pending`, disconnects, and records rows with `source = probe`. It sends no prompt and spends no tokens.

- The probe runs on every host's daemon against that host's local scratch mirror of the account config (via `account_auth.helper_env`), never the real config dir, so it cannot disturb a concurrent login or live session.
- Budgets: 25 s probe settle per account, 33 s per-host deadline, 35 s for the whole endpoint. Still-pending servers are recorded as `pending`.
- A host that cannot finish reports a skipped reason: `timed out`, `budget exhausted`, or `unknown account`.
- Account names in the request are checked against the known account set; anything else is rejected with 400 `unknown_account`.
- One probe per account at a time; a second caller awaits the first.

## Reauth follow-through

**Reauth** on a `needs-auth` row opens the existing paste-back `McpAuthFlow`. On success the edge:

1. Syncs only the `mcpOAuth` key of each account's `.credentials.json` (`sync_cfg_mcp_oauth`), with a stat guard against a concurrent CLI write, using the same merge policy as `merge_shared_mcp`. The dir receives the bundle's public view, which has no refresh token.
2. Reconnects that server in every live session whose latest row for it is `needs-auth` or `failed` (bounded fan-out: 4 concurrent, 15 s each).
3. Re-probes the account.

Reconnect failures are logged and show up in the next status poll; they never fail the login response.

## Token refresh

`store/mcp.shared.json` is the only file that holds MCP refresh tokens, so no session's CLI refreshes a shared grant. On the refresh authority, the edge refreshes each bundle that expires within 10 minutes. It checks every 60 s, writes the result back compare-and-swap, and publishes the public view to every Anthropic account dir. Session start (`compose`) uses the same sync, so it never rolls back a rotation and never writes a refresh token into a dir. A dead grant (`invalid_grant` and the CLI's equivalents) is cleared to the CLI's signed-out shape and needs a Reauth. Any other failure is retried on the next tick.

**Kill switch** - `VIBERPIT_MCP_OAUTH_REFRESH_ENABLED` (default on). When off, nothing refreshes MCP grants, and a token stays valid only until it expires.

## In-session readout

A compact `MCP n/m` (connected of total) sits beside the context meter in the session header, only when the session has at least one user MCP server. It is muted when all are connected and warning-coloured when any is failed or needs-auth. Its popover lists each server's status, error and tool count with:

- **Reconnect** - `POST /api/sessions/{id}/mcp/{server}/reconnect` (audited as `session.mcp_reconnect`)
- **Reauth** - opens `McpAuthFlow` in place
- **Manage** - links to Settings › MCP

Data loads from `GET /api/sessions/{id}/mcp` on mount, then follows `mcpStatus` session events. The internal ViberPit app server is hidden (filtered by name).

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

**Live toggle** - the in-session `MCP n/m` popover gets an on/off switch per server (`POST /api/sessions/{id}/mcp/{server}/toggle`, `{enabled}`; 400 unknown server, 409 if the CLI refuses, 404 if the session is not live). The choice is saved to `sessions.mcp_overrides` and survives resume; the driver's enforcement set is kept in sync. The DB is authoritative: the CLI also mirrors toggles into `.claude.json`, but that copy is overwritten at the next driver build. Disabled servers are excluded from the `n/m` count.

**Repo key** - repo rules key on the session's stored repo (`Session.repo`, set at creation), not the live git remote some UI labels show. Adopted and explicit-cwd sessions have no stored repo and get server defaults only.

**Switch coverage** - the popover shows a switch only for user-scope and plugin servers (CLI scope `user` or `dynamic`); `project`, `claudeai` and `local` rows keep status, Reconnect and Reauth but no switch, since the toggle route rejects them.

**Applies to new and resumed sessions** - changing a repo rule or server default does not touch sessions that are already running. Use the live toggle for those.

**Kill switch** - `VIBERPIT_MCP_SCOPE_ENABLED` (default on). When off, no list is written and both the rules API and the toggle route return 404 `{"error": "mcp_scope_disabled"}`; Settings hides its scope controls immediately; the session popover hides its switches only after the first toggle returns 404 `mcp_scope_disabled`. Post-start enforcement runs from the MCP status poll, so it also requires `VIBERPIT_MCP_STATUS_ENABLED` on; with status off only the pre-launch list protects a session.

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
