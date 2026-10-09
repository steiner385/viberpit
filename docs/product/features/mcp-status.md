---
slug: mcp-status
parent: config-library-snapshots
title: MCP Live Status and Reauth
state: live
links:
  specs:
    - docs/superpowers/specs/2026-10-09-mcp-live-status-design.md
  code:
    - server/mcp_health/probe.py
    - server/mcp_health/store.py
    - server/mcp_health/status.py
    - server/mcp_health/after_login.py
    - server/api/library.py
    - server/api/sessions.py
    - server/sessions/claude_driver.py
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

1. Refreshes only the `mcpOAuth` key of each account's `.credentials.json`, with a stat guard against a concurrent CLI write, using the same merge policy as `merge_shared_mcp`.
2. Reconnects that server in every live session whose latest row for it is `needs-auth` or `failed` (bounded fan-out: 4 concurrent, 15 s each).
3. Re-probes the account.

Reconnect failures are logged and show up in the next status poll; they never fail the login response.

## In-session readout

A compact `MCP n/m` (connected of total) sits beside the context meter in the session header, only when the session has at least one user MCP server. It is muted when all are connected and warning-coloured when any is failed or needs-auth. Its popover lists each server's status, error and tool count with:

- **Reconnect** - `POST /api/sessions/{id}/mcp/{server}/reconnect` (audited as `session.mcp_reconnect`)
- **Reauth** - opens `McpAuthFlow` in place
- **Manage** - links to Settings › MCP

Data loads from `GET /api/sessions/{id}/mcp` on mount, then follows `mcpStatus` session events. The internal ViberPit app server is hidden (filtered by name).

## Limits and switches

- **Codex** sessions do not report MCP status yet; the readout is absent and the endpoints return an empty list. Planned in sub-project 4 of the MCP management series.
- **Kill switch** `VIBERPIT_MCP_STATUS_ENABLED` (default on) disables status reporting, persistence, the probe and the readout; the API then returns empty status lists.

## Data model

Table `mcp_server_status` (model `McpServerHealth`, migration `0078`), unique on `(account, host, server)`; each row is the latest observation. Columns: `status`, `error` (sanitized), `tool_count`, `scope`, `source` (`session` | `probe`), `session_id`, `observed_at` (`db_utc_now()`). It replaces the old cache-file reader for needs-auth, which was deleted.
