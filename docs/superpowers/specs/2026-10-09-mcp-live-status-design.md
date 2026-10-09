# MCP live status + reauth — design

**Date:** 2026-10-09 · **Status:** draft for review · **Sub-project 1 of 4** of "integrated MCP server management"

## Context

Settings › MCP servers (`web/src/features/settings/sections/McpSection.tsx`, `server/api/library.py`,
`server/library/{scan,mutate}.py`) already supports add / edit / delete of user-scope servers and a
paste-back OAuth login (`server/mcp_auth.py`, `McpAuthManager`). What it cannot tell you is whether a
server **works**: needs-auth is inferred by polling the CLI's per-account `mcp-needs-auth-cache.json`,
the `mcp_servers` block of the SDK init message is discarded (`server/sessions/translator.py`), and no
live session ever learns that a login just succeeded.

The SDK (`claude-agent-sdk`) exposes `ClaudeSDKClient.get_mcp_status()` (per-server `status ∈ {connected,
failed, needs-auth, pending, disabled}`, `error`, `tools`, `serverInfo`, `scope`, `config`),
`reconnect_mcp_server(name)` and `toggle_mcp_server(name, enabled)`. None are used today.

### Decomposition (agreed 2026-10-09)

1. **Live status + reauth** — this spec.
2. Scope model — per-project / per-session enable/disable (`projects[path].mcpServers`, `enabledMcpjsonServers`).
3. Catalog — browse the official MCP registry + plugin-bundled servers; install with prefilled config into a scope from (2).
4. Codex parity — render the same definitions into Codex config; feed `mcpServer/startupStatus` into this spec's surface.

Each gets its own spec → plan → PRs.

## Goals

- See, per server × account × host, whether a configured MCP server is connected, failed (with the
  error), needs auth, or has never been checked.
- Check a server on demand ("Test") without opening a session and without spending model tokens.
- Re-authenticate from Settings **or** from inside a session, and have live sessions pick the new
  token up without a restart.
- Reconnect a failed server inside a live session.

## Non-goals

Per-project/per-session enablement (sub-project 2), catalog/install (3), Codex sessions (4), proactive
notifications or needs-you items on status flips (explicitly not chosen), background periodic probing.

## Design

### 1. Engine contract and driver

- `AgentCapabilities` (`server/sessions/engine.py`) gains `mcp_status: bool`. Claude declares `True`;
  Codex `False` until sub-project 4. The `AgentDriver` Protocol gains `mcp_status()` and
  `reconnect_mcp_server(name)`.
- `ClaudeAgentDriver` implements both over `self._client`, with the same `self._client is None or
  self._stream_ended` guard as `context_usage()`.
- The driver polls `get_mcp_status()`:
  - after `start()`;
  - while any server is `pending`, every 5 s, giving up after 30 s total;
  - at each turn end, beside `_emit_context_usage`;
  - after `reconnect_mcp_server`.
- Each poll emits a new normalized event (`server/sessions/events.py`):
  `McpStatus(servers=[McpServerState(name, status, error, tool_count, scope)])`, wire type `mcpStatus`.
- **Sanitization (security, Principle IV):** the SDK `config` field is never copied (it carries headers
  and env secrets); `error` is truncated to 500 chars with URL query strings redacted; the internal
  app server (`viberpit.brand` wire name) and any `scope == "sdk"` entry are filtered out.
- Poll failures (control request unsupported, timeout, stream ended) log at **DEBUG** — not WARNING, which
  would auto-file an issue — and emit nothing.

### 2. Persistence — single source of truth

New model `McpServerStatus`, table `mcp_server_status`, unique on `(account, host, server)`:

| column | type | notes |
|---|---|---|
| `account` | str | account name |
| `host` | str | daemon host that observed it (a stdio command may exist on one host only) |
| `server` | str | name as the CLI reports it (`plugin:<p>:<s>` for plugin servers) |
| `status` | str | one of the SDK statuses |
| `error` | str, nullable | sanitized |
| `tool_count` | int, nullable | |
| `scope` | str, nullable | |
| `source` | str | `session` or `probe` |
| `session_id` | str, nullable | the reporting session, when `source == session` |
| `observed_at` | datetime | `db_utc_now()` — never `func.now()` |

- All new columns are nullable except the key and `status`/`source`/`observed_at`. Migration
  `0078_mcp_server_status` chains off `0077_personal_quick_replies`. The model is added to
  `_REQUIRED_EXTRAS` (`test_postgres_compat.py`) and the Postgres suite is run.
- The daemon upserts through an `on_mcp_status` driver callback wired by the orchestrator, mirroring
  `on_context_usage` / `_persist_context_usage`.
- **Replaces** `server/library/scan.py:_needs_auth_by_server()`: `list_mcp_servers()` and the
  `McpAuthManager` account picker (`default_account_picker`) read needs-auth from this table. The
  cache-file reader is deleted in the same PR (Principle VI). `McpAuthManager` keeps clearing the CLI's
  own cache files on login success, which is CLI behaviour, not a ViberPit status source.

### 3. Probe ("Test connection")

- New module `server/mcp_health/probe.py`: open a `ClaudeSDKClient` whose `CLAUDE_CONFIG_DIR` is the
  account's composed cfg dir (`configdir.compose(name)`, as `McpAuthManager` uses), cwd a neutral
  helper dir, **no** `strict_mcp_config`, no prompt sent. Poll `get_mcp_status()` until nothing is
  `pending` or 20 s elapse, disconnect, upsert rows with `source = probe` (still-pending servers are
  recorded as `pending`).
- Runs on the daemon that owns the cfg dirs, via a new `probe_mcp(account=None, server=None)` RPC;
  in-process in single-host mode. `account=None` probes every Anthropic account on that host.
- Concurrency: one probe per account at a time (a second caller awaits the first's result), and a
  global cap of 2.
- Scope: user-scope + plugin servers only; project `.mcp.json` servers belong to sub-project 2.
- Must be verified against the real binary to cost zero model tokens (see Testing).

### 4. API

- `GET /api/library/mcp` — each server gains
  `status: [{account, host, status, error, toolCount, observedAt, source}]`, **replacing** `needsAuth`
  (web consumers updated in the same PR).
- `POST /api/library/mcp/test` `{account?, server?}` — runs the probe synchronously (≤ 30 s budget),
  returns the refreshed rows. `server` narrows the returned rows; the probe connects all servers either
  way.
- `GET /api/sessions/{id}/mcp` — fresh `driver.mcp_status()` for a live session (404 when not live,
  empty list when the engine lacks the capability). Used on mount, so no replay-on-attach plumbing.
- `POST /api/sessions/{id}/mcp/{server}/reconnect` — follows the `interrupt` chain:
  `session_actions` → facade → `FleetOrchestrator._PER_SESSION` → `RemoteOrchestrator` → `_SIMPLE`
  RPC → `Orchestrator` → driver. Audited as `session.mcp_reconnect`.
- On `McpAuthManager` success: the edge reconnects that server in every live session whose latest row
  for it is `needs-auth` or `failed`, then re-probes the account. Reconnect failures are logged and
  surfaced through the next `McpStatus` poll, never raised into the login response.

### 5. UI

- **Settings › MCP** (`McpSection.tsx`):
  - Each server row gets a status pill showing the worst status across its rows, in the order
    failed > needs-auth > pending > connected; `disabled` is shown neutral.
  - With no rows, the pill reads "Not checked yet".
  - Expanding a row shows a per-account × per-host list: the status, the error, and
    "checked 3 m ago · from session/probe".
  - Actions: **Test** per server, **Test all** in the section header, and **Reauth** on any
    `needs-auth` row, which opens the existing `McpAuthFlow`.
- **In-session**: a compact `MCP n/m` readout in `SessionMeta.tsx` beside ctx, present only when the
  session has ≥1 user MCP server. It is muted when all are connected, and warning-coloured when any is
  failed or needs-auth.
  - Its popover lists each server's status, error and tool count, with **Reconnect**, **Reauth** and
    **Manage →** (links to `/settings/mcp`).
  - Data comes from `GET /api/sessions/{id}/mcp` on mount, then from `mcpStatus` events through the
    reducer (`web/src/session/{events,reducer}.ts`, including `SESSION_EVENT_TYPE_FLAGS`).
- Kill switch `VIBERPIT_MCP_STATUS_ENABLED` (default on) disables polling, persistence, the probe and the
  readout; the API then returns empty status lists.

## Error handling

- The SDK lacks the control request (older CLI): the driver latches "unsupported" after the first failure
  and stops polling for that session.
- A probe times out: rows for still-`pending` servers are written as `pending` with
  `error = "timed out after 20s"`, and the endpoint returns 200 with those rows.
- A probe hits an account with no usable credentials: it is skipped, and the response carries
  `skipped: [{account, reason}]`.
- A reconnect targets a session that is no longer live: 404. Reconnecting an unknown server name
  returns the SDK's error as 409.

## Open risk (verify first in the plan)

Remote worker hosts (rocky) receive credentials as envelopes pushed from the edge
(`server/ipc/creds_rpc.py`). If `mcpOAuth` from `store/mcp.shared.json` is not part of those envelopes,
then reconnecting a rocky session after reauth will not see the new token. If so, the fix is to include
it in the envelope, not to work around it here.

## Testing

- Driver (fake client):
  - pending-settle loop and its 30 s cap;
  - turn-end poll;
  - `config` stripped, error redaction, internal server filtered;
  - DEBUG-only failure path and the unsupported latch.
- Persistence: upsert on SQLite plus the Postgres suite (`VIBERPIT_TEST_PG_URL`); `observed_at` comes
  from `db_utc_now()`.
- Probe (fake SDK factory): settle, timeout, per-account dedupe, global cap.
- RPC chain: the `_PER_SESSION` completeness test plus both sessions-facade doubles (`FakeOrch`,
  `_FakeSessions`).
- `list_mcp_servers` / picker: derive needs-auth from rows; no reference to the cache file remains.
- Web:
  - reducer `mcpStatus` case;
  - `SessionMeta` readout states;
  - `McpSection` pill, expansion and Test;
  - the full `npm run test`, plus a grep of `web/e2e` for the changed `needsAuth` copy and a run of the
    settings e2e spec.
- **Real binary (Principle III)**, run once against CLI 2.1.295 before building on the assumption:
  - `get_mcp_status()` reports `needs-auth` for an unauthenticated HTTP server, `failed` with an error
    for a bad stdio command, and `pending` immediately after connect;
  - a connected client that is never sent a prompt records no usage;
  - `reconnect_mcp_server()` after a token lands moves `needs-auth` to `connected` without a restart.
