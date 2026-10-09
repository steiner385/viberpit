# MCP repo-scope verification spike (2026-10-09)

Principle III gate for sub-project 2a (per-repo MCP scope + live session toggle): answers observed against the real
binary, not assumed.

## Versions

- Claude Code CLI: `2.1.295 (Claude Code)` (`~/.local/share/claude/versions/2.1.295`, passed to the SDK as `cli_path`)
- `claude-agent-sdk`: `0.2.105` (deploy venv `.venv-py314`)
- Host: dobby

## Method

Throwaway scratch config dir under `/tmp/mcp_scope_spike` (mode 0700, deleted afterwards), seeded by `cp -L` of one
Anthropic account's `.claude.json` and `.credentials.json`; scrubbed env (`env -i`, scratch `HOME`). No real cfg dir or
`store/*` file was written. Scratch additions: `spike-ok` (user scope, `npx -y @modelcontextprotocol/server-everything`,
which started fine), one real plugin (`desktop-commander`, copied with its marketplace dir; the plugin loads only when
the marketplace dir is present), and a cwd with a project `.mcp.json` server `proj-srv` (same npx server). The account's
own `aws-oauth` user server was also present. Each run rewrote `projects[<cwd>]` with
`hasTrustDialogAccepted: true` and the `disabledMcpServers` under test.

## Results

| # | Question | Expected | Observed |
|---|---|---|---|
| 1 | `disabledMcpServers: ["spike-ok", "plugin:<p>:<s>"]` -> status at start | both `disabled`, no `needs-auth`, no OAuth prompt | **Matches.** Run 1 (empty list) exposed the key `plugin:desktop-commander:desktop-commander`; run 2 with both names listed: both `disabled` at the first poll (no `pending`), `aws-oauth` unaffected and `connected`. No `needs-auth`, no error. |
| 2 | Plugin key format: `<p>` = plugin name without `@marketplace`? | `plugin:<plugin-name>:<server>` | **Matches.** Installed as `desktop-commander@claude-plugins-official`; key is `plugin:desktop-commander:desktop-commander` (no `@marketplace`). Scope `dynamic`. The key is accepted verbatim in `disabledMcpServers` and by `toggle_mcp_server`. |
| 3 | `toggle_mcp_server("spike-ok", True)` while disabled | `connected` | **Matches.** `connected` on the next poll (tools available, scope `user`). The plugin key toggles identically (`disabled` -> `connected`). |
| 4 | `toggle_mcp_server("spike-ok", False)` | `disabled` | **Matches.** `disabled`; plugin key likewise. |
| 5 | `projects[cwd].disabledMcpServers` after the run | record it | **The CLI persists every toggle into `.claude.json`.** Starting `["spike-ok", "plugin:..."]`: after enable -> `["plugin:desktop-commander:desktop-commander"]`; after disable -> `["plugin:desktop-commander:desktop-commander", "spike-ok"]` (removed on enable, appended on disable, so ORDER CHANGES). Final persisted value equals the live disabled set. |
| 6 | Project `.mcp.json` server: `disabledMcpServers` or `disabledMcpjsonServers`? | record it | **`disabledMcpServers` works.** `proj-srv` (scope `project`, name is the bare key) was `connected` with an empty list and `disabled` with `["proj-srv"]`. Headless it connected even though `enabledMcpjsonServers` was empty/unset (no approval gate observed). Setting only `disabledMcpjsonServers: ["proj-srv"]` made the server absent from `get_mcp_status` entirely (not `disabled`), so the status surface can't show it. 2b should use `disabledMcpServers`. |
| 7 | Toggling an unconfigured name | record | Plain `Exception`, message `Server not found: no-such-server`. Status list and persisted file unchanged. |

## Log excerpt (redacted; rows are name/status/scope only)

```
=== RUN1b empty
start [plugin:desktop-commander:desktop-commander connected/dynamic, aws-oauth connected/user, spike-ok connected/user]
=== RUN2 disabled spike-ok + plugin, toggle spike-ok
start [plugin:desktop-commander:desktop-commander disabled/dynamic, aws-oauth connected/user, spike-ok disabled/user]
after enable  [... plugin disabled, aws-oauth connected, spike-ok connected]
persisted-mid ["plugin:desktop-commander:desktop-commander"]
after disable [... plugin disabled, aws-oauth connected, spike-ok disabled]
persisted-mid ["plugin:desktop-commander:desktop-commander", "spike-ok"]
bogus exc Exception Server not found: no-such-server
=== Q6c approved + disabledMcpServers=[proj-srv]
start [... proj-srv disabled/project]
=== Q6d disabledMcpjsonServers=[proj-srv] only
start [plugin connected, aws-oauth connected, spike-ok connected]      (proj-srv absent)
```

## Other observations

- The CLI rewrites the `projects[cwd]` entry itself while running: after runs where the spike had written
  `enabledMcpjsonServers` / `disabledMcpjsonServers`, those keys read back `null`. Only `disabledMcpServers` is
  reliably round-tripped. Do not rely on other project keys surviving a CLI session.
- No server was `pending` at the first poll in this run (all local/cached), so the 15 s settle finding of spike 1 was
  not re-exercised. The plugin loaded only because its marketplace dir existed in the scratch cfg.

## Implications for the plan

1. **Q1/Q2 match Expected, so no STOP.** Task 3's plugin `server_key` is `plugin:<plugin-name>:<server-name>` where
   `<plugin-name>` is the installed plugin id with the `@marketplace` suffix stripped. Use the status-list `name`
   as the source of truth where available.
2. **The CLI persists toggles (Q5).** A live toggle through `toggle_mcp_server` already writes
   `projects[cwd].disabledMcpServers` itself, so the daemon/edge must not write a second, competing copy for the
   same cwd (parallel source of truth; last writer wins, and the CLI rewrites the whole project entry). Any
   repo-scope defaults ViberPit seeds must be written BEFORE the session starts (as the spike did), and any
   comparison against ViberPit's stored list must be order-insensitive (set comparison), since the CLI reorders.
3. **Per-repo scope is keyed by cwd** in `.claude.json`, which is the CLI's file in the account cfg dir and is shared
   across all sessions of that account in that cwd: a live toggle in one session changes the persisted default for
   the next session in the same repo. Tasks that model "session toggle" vs "repo default" must account for this.
4. **Task 7-ish unknown-name handling (Q7):** the error is a bare `Exception("Server not found: <name>")`;
   map it by message, not type.
5. **2b input (Q6):** project `.mcp.json` servers are controlled by `disabledMcpServers` using the bare server key;
   `disabledMcpjsonServers` hides them from status entirely.
6. **Spec caveat for plugin servers:** per spike 1 the CLI caches plugin-server failures for 15 min; a disabled ->
   enabled plugin toggle may report "Skipping connection..." and need `reconnect_mcp_server`.
