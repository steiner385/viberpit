# MCP status verification spike (2026-10-09)

Principle III gate for the "MCP live status + reauth" plan: answers observed against the real binary, not assumed.

## Versions

- Claude Code CLI: `2.1.295 (Claude Code)` (`~/.local/share/claude/versions/2.1.295`, passed to the SDK as `cli_path`; no `claude` on PATH)
- `claude-agent-sdk`: `0.2.105` (deploy venv `.venv-py314`)
- Host: dobby (account store is local ext4; rocky mounts it via sshfs)

## Method (deviations from the brief, per controller ruling R1)

Everything ran against a throwaway scratch config dir `/tmp/mcp_spike/cfg` (since deleted), seeded from the
`tony_steinonline.net` account (`.claude.json` and `.credentials.json` copied with `cp -L`). No live cfg dir or
`store/*` file was touched. Added to the scratch `.claude.json` only: `spike-broken` (stdio, `/nonexistent/mcp-binary`)
and `spike-http` (`https://mcp.notion.com/mcp`, no token). The scratch had one real plugin
(`desktop-commander`, copied, only plugin enabled) for Q4, and an in-process SDK server `spikesdk` (one `ping` tool)
mounted via `create_sdk_mcp_server` for Q5. Q7 was simulated (see below) instead of a real OAuth login.
Q6 skipped the `/api/usage` comparison; only transcript files were checked.

## Results

| # | Question | Expected | Observed |
|---|---|---|---|
| 1 | Status for an unauthenticated HTTP server | `needs-auth` | `needs-auth` (`spike-http`, settled by t=2.5 s; `error: null`) |
| 2 | Status and `error` for a stdio server whose command doesn't exist | `failed`, non-empty error | `failed`, `error: "ENOENT: no such file or directory, posix_spawn '/nonexistent/mcp-binary'"`; already `failed` at t=0.0 |
| 3 | Any server `pending` at t~0; when does it settle? | some `pending`, settled < 20 s | Yes: plugin, `aws-oauth`, `spike-http` all `pending` at t=0.0. Last one settled at t=15.0 s (`aws-oauth` connected at 12.5 s, plugin server failed at 15.0 s). Settled < 20 s, but only just for a slow server; a failing/slow plugin server is the long pole |
| 4 | Name format for a plugin-bundled server | `plugin:<plugin>:<server>` | `plugin:desktop-commander:desktop-commander` (matches); `scope` is `dynamic` |
| 5 | `scope` for the in-process SDK server | `sdk` | **CONTRADICTS: `dynamic`** (`spikesdk`, `status: connected`, `tools: 1`). Plugin servers also report `dynamic`; user-config servers report `user`. No `sdk` scope seen. SDK server first appeared in the list at t=2.5 s, not t=0 |
| 6 | No usage recorded / no transcript | unchanged / none | No transcript: zero `*.jsonl` anywhere under the scratch dir after both runs, and no `projects/` dir created. `/api/usage` comparison skipped (R1) |
| 7 | After OAuth completes elsewhere and `mcpOAuth` is copied into this cfg's `.credentials.json` while connected, does `reconnect_mcp_server` make it `connected`? | `connected` | `connected` (simulated: removed both `aws-oauth|...` entries from scratch `.credentials.json` before connecting -> `needs-auth`; wrote them back while the client was still connected; `reconnect_mcp_server("aws-oauth")` -> `connected`, 8 tools, ~10 s after the write/reconnect call). Caveat: simulation by removal/restoration, not a real cross-account OAuth login |
| 8 | `mcpOAuth` in `server/ipc/creds_rpc.py` / `server/sessions/claude_authority.py`; rocky account store is the sshfs mount | none; sshfs | grep: no matches (rc=1). `ssh rocky findmnt -T /home/tony/.claude-accounts` -> `tony@100.102.11.121:/home/tony/.claude-accounts  fuse.sshfs  rw,...`; on dobby the same path is local ext4. So worker cfg dirs ARE the shared files. (`mcpOAuth` does appear in `server/mcp_auth.py` and `src/viberpit/creds.py`, outside the Q8 grep scope.) |

## Log excerpts (secrets: none present; rows contain only name/status/error/scope/tool count)

Run 1 (`/tmp/mcp-spike.log`), t=0.0 and last rows:

```
0.0 [plugin:desktop-commander:desktop-commander pending/dynamic, aws-oauth pending/user, spike-broken failed/user, spike-http pending/user]
```

Verbatim t=0.0, 2.5 and 15.0 rows (JSON as printed):

```
0.0 [{"name": "plugin:desktop-commander:desktop-commander", "status": "pending", "error": null, "scope": "dynamic", "tools": 0}, {"name": "aws-oauth", "status": "pending", "error": null, "scope": "user", "tools": 0}, {"name": "spike-broken", "status": "failed", "error": "ENOENT: no such file or directory, posix_spawn '/nonexistent/mcp-binary'", "scope": "user", "tools": 0}, {"name": "spike-http", "status": "pending", "error": null, "scope": "user", "tools": 0}]
2.5 [{"name": "plugin:desktop-commander:desktop-commander", "status": "pending", "error": null, "scope": "dynamic", "tools": 0}, {"name": "aws-oauth", "status": "pending", "error": null, "scope": "user", "tools": 0}, {"name": "spike-broken", "status": "failed", "error": "ENOENT: no such file or directory, posix_spawn '/nonexistent/mcp-binary'", "scope": "user", "tools": 0}, {"name": "spike-http", "status": "needs-auth", "error": null, "scope": "user", "tools": 0}, {"name": "spikesdk", "status": "connected", "error": null, "scope": "dynamic", "tools": 1}]
15.0 [{"name": "plugin:desktop-commander:desktop-commander", "status": "failed", "error": "MCP error -32000: Connection closed", "scope": "dynamic", "tools": 0}, {"name": "aws-oauth", "status": "connected", "error": null, "scope": "user", "tools": 8}, {"name": "spike-broken", "status": "failed", "error": "ENOENT: no such file or directory, posix_spawn '/nonexistent/mcp-binary'", "scope": "user", "tools": 0}, {"name": "spike-http", "status": "needs-auth", "error": null, "scope": "user", "tools": 0}, {"name": "spikesdk", "status": "connected", "error": null, "scope": "dynamic", "tools": 1}]
```

Run 2 (Q7 simulation, `SIM_OAUTH=aws-oauth`, reconnect arg `aws-oauth`):

```
simulation: removed ['aws-oauth|751831b0c9c1bf84', 'aws-oauth|c6c1c83162f8ce2d']
2.5 ... {"name": "aws-oauth", "status": "needs-auth", "error": null, "scope": "user", "tools": 0} ...
simulation: restored entries into creds while connected
after reconnect 12.8 ... {"name": "aws-oauth", "status": "connected", "error": null, "scope": "user", "tools": 8} ...
```

## Other observations

- Run 2 showed the plugin server as `failed` with `error: "Skipping connection (recent failure cached; retries automatically in 15 min, or edit the plugin config to retry now)"` at t=0.0: the CLI caches plugin-server failures across processes (state outside the cfg dir's `.claude.json`). A status UI must expect this `error` string, and a "reconnect" may be needed to force a retry.
- The `desktop-commander` plugin server failing with `MCP error -32000: Connection closed` is an artifact of the sandboxed scratch run (it also fails in the live session today), not a finding about the status API.
- `get_mcp_status` rows carry `scope` values `user` and `dynamic` only in this run.
- `reconnect_mcp_server` returns after the reconnect settles (call took ~10 s here); the subsequent status was already final.

## Implications for the plan

1. **Q5 contradicts Expected.** The in-process SDK server reports `scope: "dynamic"`, not `sdk`, and plugin servers share `dynamic`. Any code that identifies the app's own SDK server (or filters it out of the user-facing list) by `scope == "sdk"` is wrong; identify it by name (the mounted server name from `viberpit.brand`) and treat plugin servers by the `plugin:` name prefix.
2. The SDK server is absent from the status list at t=0 and appears by ~2.5 s; do not treat its absence early on as "not mounted".
3. Q3 settle time (15 s worst case, driven by a slow/failing server) leaves little margin under a 20 s budget; poll with a ceiling well above 20 s and render per-server `pending` rather than waiting for all to settle.
4. Q7 holds (under simulation): copying `mcpOAuth` into the cfg's `.credentials.json` and calling `reconnect_mcp_server` flips `needs-auth` to `connected` without restarting the client. A real cross-account OAuth round trip was not exercised.
5. Q1/Q2/Q4/Q8 match Expected; no `mcpOAuth` handling exists in `creds_rpc.py`/`claude_authority.py`, and rocky shares the dobby store via sshfs.
