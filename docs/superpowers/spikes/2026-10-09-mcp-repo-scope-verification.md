# MCP repo-scope verification spike (2026-10-09)

Principle III gate for sub-project 2a (per-repo MCP scope + live session toggle): answers observed against the real
binary, not assumed.

## Versions

- Claude Code CLI: `2.1.295 (Claude Code)` (`~/.local/share/claude/versions/2.1.295`, passed to the SDK as `cli_path`)
- `claude-agent-sdk`: `0.2.105` (deploy venv `.venv-py314`)
- Host: the account-store host (single machine; no sshfs worker involved)

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
| 3 | `toggle_mcp_server("spike-ok", True)` while disabled | `connected` | **Matches.** `connected` on the next poll (scope `user`; tool count was not recorded). The plugin key toggles identically (`disabled` -> `connected`; RUN3 excerpt). |
| 4 | `toggle_mcp_server("spike-ok", False)` | `disabled` | **Matches.** `disabled`; plugin key likewise (RUN3 excerpt). |
| 5 | `projects[cwd].disabledMcpServers` after the run | record it | **The CLI persists every toggle into `.claude.json`.** Starting `["spike-ok", "plugin:..."]`: after enable -> `["plugin:desktop-commander:desktop-commander"]`; after disable -> `["plugin:desktop-commander:desktop-commander", "spike-ok"]` (removed on enable, appended on disable, so ORDER CHANGES). Final persisted value equals the live disabled set. |
| 6 | Project `.mcp.json` server: `disabledMcpServers` or `disabledMcpjsonServers`? | record it | **`disabledMcpServers` works.** `proj-srv` (scope `project`, name is the bare key) was `connected` with an empty list and `disabled` with `["proj-srv"]`. Headless it connected even though `enabledMcpjsonServers` was empty/unset (no approval gate observed). Setting only `disabledMcpjsonServers: ["proj-srv"]` made the server absent from `get_mcp_status` entirely (not `disabled`), so the status surface can't show it. 2b should use `disabledMcpServers`. |
| 7 | Toggling an unconfigured name | record | Plain `Exception`, message `Server not found: no-such-server`. Status list and persisted `disabledMcpServers` unchanged by the failed call (RUN2 `after bogus` and final `persisted` lines). |

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
after bogus [plugin disabled/dynamic, aws-oauth connected/user, spike-ok disabled/user]
persisted {"disabledMcpServers": ["plugin:desktop-commander:desktop-commander", "spike-ok"], "disabledMcpjsonServers": null, "enabledMcpjsonServers": null}
=== RUN3 toggle plugin key (disabled -> enable -> disable)
start [plugin disabled/dynamic, aws-oauth connected, spike-ok connected]
after enable  [plugin connected/dynamic, ...]      persisted-mid []
after disable [plugin disabled/dynamic, ...]       persisted-mid ["plugin:desktop-commander:desktop-commander"]
=== Q6b .mcp.json present, enabledMcpjsonServers unset/empty, nothing disabled
start [... proj-srv connected/project]
=== Q6c approved + disabledMcpServers=[proj-srv]
start [... proj-srv disabled/project]
=== Q6d disabledMcpjsonServers=[proj-srv] only
start [plugin connected, aws-oauth connected, spike-ok connected]      (proj-srv absent)
```

## Other observations

- The CLI rewrites the `projects[cwd]` entry itself while running: after runs where the spike had written
  `enabledMcpjsonServers` / `disabledMcpjsonServers`, those keys read back `null`. Only `disabledMcpServers` is
  reliably round-tripped. Do not rely on other project keys surviving a CLI session.
- `hasTrustDialogAccepted`: NOT observed. I only read back the three MCP keys after each run, so I cannot say whether the
  CLI drops it. If it does, that is harmless: `configdir.trust_project` (`src/viberpit/configdir.py:140-157`) re-adds it
  at every driver build, before the CLI starts.
- No server was `pending` at the first poll in this run (all local/cached), so the 15 s settle finding of spike 1 was
  not re-exercised. The plugin loaded only because its marketplace dir existed in the scratch cfg.

## Implications for the plan

1. **Q1/Q2 match Expected, so no STOP.** Task 3's plugin `server_key` is `plugin:<plugin-name>:<server-name>` where
   `<plugin-name>` is the installed plugin id with the `@marketplace` suffix stripped. Use the status-list `name`
   as the source of truth where available.
2. **The CLI persists toggles (Q5); ruling R2: the DB stays authoritative.** Repo rules plus `sessions.mcp_overrides`
   are the source of truth. The CLI's own runtime write of `projects[cwd].disabledMcpServers` on a live toggle is a
   transient mirror, not a second authority. ViberPit overwrites that key at every driver build via
   `configdir.trust_project`, before the CLI starts, so Q5 and the plan agree. (`trust_project` will write the key
   as Task 4 behaviour; today it does not. With `disabled_mcp=None` it leaves the key untouched.)
   - Task 5's toggle route writes the DB only and never `.claude.json`.
   - The Global Constraint "written by `trust_project` only" means "ViberPit writes it only via `trust_project`";
     the CLI also writes it at runtime.
   - The CLI reorders the list (enable removes, disable appends), so any comparison of the persisted list is a set
     comparison.
3. **Stale-list hazard and ruling R3.** If ViberPit writes no list (`disabled_mcp=None`, a rules-read failure, or the
   kill switch off), a list the CLI persisted earlier could in principle linger and keep servers disabled. R3: no list
   is written in those three cases, and a stale CLI-persisted list does not survive into other sessions, for two
   reasons confirmed in code:
   - `compose()` rewrites the account `.claude.json` on every launch (`src/viberpit/configdir.py:186`, via
     `compose_claude_json`, lines 104-137). That file is built from `store/<name>.claude.json` (written only at login
     capture, `src/viberpit/auth_login.py:365`) plus the shared overlay (`overlay_shared_claude_json`, lines 90-101),
     so CLI runtime writes into `cfg/.claude.json` are discarded. Nothing in `src/` or `server/` copies them back.
   - Git-repo sessions get a unique worktree cwd (`server/sessions/orchestrator.py:5159-5170` creates one only when
     `worktrees.is_git_repo(cwd)`), so their `projects[cwd]` entry is not reused across sessions. This reason does
     NOT cover non-git cwds (see R4); reason 1 above still holds for them.
   Caveat: `trust_project` merges into the existing entry and keeps its other keys (lines 150-157), so within a single
   launch cycle an entry the CLI wrote earlier is preserved unless ViberPit overwrites `disabledMcpServers` (R2 does).
   The code does not contradict R3.
   **R4 (known limitation).** Sessions with a non-git cwd (rare: adopted or explicit-cwd sessions) use the given cwd as-is,
   so they share one `projects[cwd]` entry and one list. Two such LIVE sessions with different overrides are
   last-writer-wins on it. Accepted; document it here and in the Task 10 feature doc.
4. **Q7 handling is Task 5.** Validate the name against the configured server keys BEFORE calling the SDK; an unknown
   name is a 400. The bare `Exception("Server not found: <name>")` is only a fallback for a server deleted
   mid-flight, matched by message not type, and maps to 409 `MCP_TOGGLE_FAILED`.
5. **2b inputs (Q6).** Project `.mcp.json` servers are controlled by `disabledMcpServers` using the bare server key
   (scope `project`). `disabledMcpjsonServers` hides them from status entirely. Headless, `.mcp.json` servers
   connected with no approval gate (`enabledMcpjsonServers` unset), so the CLI will start any project `.mcp.json`
   server unless `disabledMcpServers` names it.
6. **Plugin failure cache.** Spike 1 (`docs/superpowers/spikes/2026-10-09-mcp-status-verification.md`, "Other
   observations") found the CLI caches plugin-server failures across processes for 15 min. A disabled -> enabled
   plugin toggle may therefore report "Skipping connection..." and need `reconnect_mcp_server`.
