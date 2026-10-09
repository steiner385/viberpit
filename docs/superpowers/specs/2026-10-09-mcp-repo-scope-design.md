# MCP per-repo scope + live session toggle: design

**Date:** 2026-10-09 · **Status:** implemented (sub-project 2a) · **Sub-project 2a** of "integrated MCP server management"
**Depends on:** sub-project 1, live status + reauth (`2026-10-09-mcp-live-status-design.md`, PR #2872)

## Context

Every Claude session loads every user-scope MCP server, because `store/claude.shared.json` → `mcpServers` is composed into each account's `.claude.json`. Plugin-bundled servers load the same way. Nothing is per-project or per-session.

ViberPit gives each session its own worktree (`<ns>/worktrees/<repo>/<short-id>`) and rewrites the account's `.claude.json` on every launch (`configdir.compose`). Only `trust_project(cwd)` writes a per-project entry, `projects[<worktree>]`, at every driver build (spawn, resume, reassign and adopt). So a path never identifies a project. The stable identity is the repo slug, `Session.repo` (`owner/name`). Scratch sessions have no repo.

The installed CLI (2.1.295) has a native per-project list: `projects[<cwd>].disabledMcpServers`. It also has `enabledMcpServers` for servers that are off by default. `toggle_mcp_server()` in `claude_agent_sdk` edits the same list mid-session. ViberPit uses none of these today.

### Decomposition

The sub-project-2 decomposition was agreed on 2026-10-09:
- **2a: per-repo scope and live session toggle.** This spec.
- **2b: committed `.mcp.json` editor.** It shows a repo's `.mcp.json` servers and edits them through a branch and PR, reusing `server/pm/map_changes_gh.py:GhWrite`. Its own spec follows 2a.

## Goals

1. **Repo-specific servers.** A server can be off by default and turned on for named repos only. Servers can also be turned off for chosen repos.
2. **Live session control.** Turn any server on or off inside a running session without a restart, with the choice kept when the session resumes.
3. **No connection cost for disabled servers.** A disabled server never connects, never prompts for OAuth and never loads tools.

## Non-goals

- Editing committed `.mcp.json` (2b).
- Product-level rules (rejected in favour of per-repo).
- Changing running sessions when a repo rule changes. Rules apply to new and resumed sessions.
- Codex (sub-project 4).

## Design

### 1. Data

Two new tables, plus one new nullable column. Migration `00NN_mcp_repo_scope` takes the next free number at plan time and chains off the newest revision.

| table | key | columns |
|---|---|---|
| `mcp_server_scope` | `server` (PK) | `default_enabled: bool` (NOT NULL), `updated_at` |
| `mcp_repo_rule` | `(repo, server)` unique | `enabled: bool` (NOT NULL), `updated_at` |

- `sessions.mcp_overrides`: nullable JSON `{server_key: bool}`.
- Server keys are the names the CLI reports: bare for user scope, `plugin:<p>:<s>` for plugin servers. Lookups between UI names and keys use the canonical `viberpit.mcp_names.short_name`.
- A server with no `mcp_server_scope` row defaults to enabled, which is today's behaviour.
- `updated_at` comes from `db_utc_now()`.

### 2. Effective state (one pure function)

`server/mcp_scope/resolve.py`:

```
effective_disabled(servers, *, server_defaults, repo_rules, session_overrides) -> list[str]
```

For each configured server, the first source that has a value wins:
1. the session override;
2. the repo rule for `Session.repo`. Scratch sessions skip this step.
3. the server default;
4. otherwise, enabled.

It returns the sorted list of disabled server keys. It is pure, with no I/O, and is the single source of truth for both the CLI list and the UI's "disabled for this repo" hints.

### 3. Applying it to the CLI

- `configdir.trust_project(cwd, *, disabled_mcp: list[str] | None = None)` writes `projects[cwd].disabledMcpServers = disabled_mcp` when given. `None` leaves the key untouched.
- The orchestrator computes the list in the one helper every driver build already passes through (`_driver_config_dir` → `trust_project`):
  - it reads the rules for `rec.repo` and `rec.mcp_overrides`;
  - it reads the configured server names from the shared store and the enabled plugins;
  - it calls `effective_disabled`;
  - all of this runs off the event loop through `account_store_io` (#2852).
- Writes stay inside the existing compose → trust → isolate ordering, so a recompose can never wipe the list after it is written.

### 4. Live toggle

- **Driver:** `ClaudeAgentDriver.toggle_mcp_server(name, enabled)` calls the SDK's `toggle_mcp_server`, then re-polls status. The Codex driver raises `NotImplementedError`, which maps to 409.
- **Orchestrator:** `mcp_toggle(session_id, server, enabled)` calls the driver, saves `{server: enabled}` into `sessions.mcp_overrides`, and returns the fresh status list.
- **Plumbing:** the same RPC chain as `mcp_reconnect`:
  - `_PER_SESSION`;
  - the daemon handler with error-shaped dicts;
  - `RemoteOrchestrator` re-raising;
  - the facade;
  - both test doubles.
- **Route:** `POST /api/sessions/{id}/mcp/{server}/toggle {enabled: bool}`, audited as `session.mcp_toggle`. Responses are 404 when the session isn't live, 409 on an SDK error, and 400 for an unknown server.

### 5. Rules API (edge)

- `GET /api/library/mcp/scope` returns `{servers: {key: {defaultEnabled}}, rules: [{repo, server, enabled}]}`.
- `PUT /api/library/mcp/scope/{server} {defaultEnabled}` sets a server's default.
- `PUT /api/library/mcp/scope/{server}/repos/{owner}/{name} {enabled}` and `DELETE` on the same path manage a repo rule.
- **Validation:**
  - `server` must be a configured server key;
  - the repo must be one ViberPit knows: the clone registry or the repos in `ProjectRepo`, whichever is the canonical "known repos" list at plan time;
  - anything else returns 400;
  - path segments are validated individually (lesson from sub-project 1's path traversal).
- **Audit:** `library_mcp_scope_saved` (name and repo only).
- **Kill switch:** `VIBERPIT_MCP_SCOPE_ENABLED`, default on. When off, no list is written, the rules API returns 404 `mcp_scope_disabled`, and the UI hides the controls.

### 6. UI

**Settings › MCP, per server row:**
- A scope control: "On for all repos" or "Off by default".
- An "Exceptions" list of repos where the server is turned the other way, with a repo picker drawn from known repos.
- Unsaved state uses the existing settings save pattern.

**Session `MCP n/m` popover:**
- Each server gets an on/off switch that calls the toggle route.
- Disabled servers show as "Disabled". `n/m` counts enabled servers only, which fixes sub-project 1 deferred minor M6.
- Turning on a server that a repo rule disabled creates a session override only; the repo rule itself is unchanged. The popover shows "Disabled" without the reason, because the status payload carries no reason and adding one isn't worth it.

**Hint:** a rule change notes "Applies to new and resumed sessions."

## Error handling

- A rules read failure at driver build logs at DEBUG and writes no list, so the session falls back to all servers on, as today. The session must start.
- A toggle on an unsupported CLI is latched like the status-poll latch and returns 409 with the SDK message via `extractMutationErrorMessage` on the web side.
- A repo or server deleted later leaves stale rule rows, which are ignored by `effective_disabled`. They are pruned when the server is deleted in Settings.

## Real-binary verification (first plan task)

On a scratch cfg copy (as in sub-project 1's spike), using CLI 2.1.295:
1. `projects[cwd].disabledMcpServers: ["<user server>", "plugin:<p>:<s>"]` → those servers report `disabled` at startup and never connect. Check that no OAuth prompt or `needs-auth` results.
2. `toggle_mcp_server(name, True|False)` mid-session flips the status. Record whether the CLI writes the result back into `projects[cwd]` of `.claude.json`.
3. Whether `disabledMcpServers` also covers project `.mcp.json` servers or whether `disabledMcpjsonServers` is needed. Record this for 2b.

If (1) fails, stop and revisit the approach. Approach B, the per-session overlay, cannot remove user-scope servers.

## Testing

- `effective_disabled`: a precedence table test covering session over repo over default over implicit, scratch sessions, plugin keys and unknown keys.
- `trust_project` writes, preserves and omits `disabledMcpServers` as specified; the compose → trust ordering keeps it.
- Orchestrator: the list is computed for spawn, resume, reassign and adopt (one helper, asserted through each path's existing test harness).
- Toggle RPC chain round trip including error shapes, and the `_PER_SESSION` completeness test.
- Rules API: validation (unknown server or repo, traversal-shaped segments), kill switch and audit.
- Postgres suite for the new tables (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`).
- Web: scope control, exceptions list, popover switch and the `n/m` count; full `npm run test`; bundle budget (lazy-load new popover weight, lesson from #2872).

## As built

Rulings made during implementation (ledger R1-R7):

- **R1** - the TS `serverKey` and Python `server_key` share one test table; key parity is the contract between UI rules and the CLI list.
- **R2** - the DB (rules plus `sessions.mcp_overrides`) is authoritative; the CLI's own write of `disabledMcpServers` on a live toggle is a transient mirror that the next driver build overwrites. The toggle route writes the DB only.
- **R3** - the stale-list hazard on "no list written" paths (kill switch off, rules read failure) is accepted, because `compose()` drops prior `projects[cwd]` entries on every launch; a test pins that behaviour.
- **R4** - sessions sharing a non-git cwd share one `projects[cwd]` list and last-writer-win; accepted as a known limitation, with a test pinning that git-repo sessions get distinct worktree keys.
- **R5** - closing the `compose()` race by post-start enforcement in the driver (`AgentDriverConfig.desired_disabled_mcp`, `toggle_mcp_server(name, False)` after the first good status poll and after `/clear`), not by changing `compose()`.
- **R6** - the kill-switch 404 on the toggle route uses the flat `{"error": "mcp_scope_disabled"}` shape, same as the rules API, so the web detects both with one code.
- **R7** - the scope and toggle hooks live in `web/src/api/mcpScopeHooks.ts`, imported only by the lazy Settings control and lazy session readout, keeping `serverKey` off the critical path; the `initialJs` budget was raised only by the measured overage.

Real-CLI findings that changed the design (spike `2026-10-09-mcp-repo-scope-verification.md`):

- **Plugin keys use the manifest name.** The CLI reports `plugin:Notion:notion`, not the registry id `plugin:notion:notion`. `scan.list_mcp_servers` exposes `pluginName` (manifest name, falling back to the registry name) and `server_key` uses it; otherwise a Notion rule would never match.
- **The CLI persists live toggles** into `projects[cwd].disabledMcpServers` (enable removes, disable appends, so order changes). Hence R2: compare as sets and keep the DB authoritative.
- **Post-start enforcement is needed** (R5): writing the list before launch is not enough when `compose()` races the CLI start.
