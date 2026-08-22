# Fix pattern: blocking I/O on the session daemon's event loop

**Date:** 2026-08-22
**Origin:** RC1 of the fleet-recovery investigation (`specs/2026-08-22-fleet-recovery-design.md`,
`plans/2026-08-22-p1-event-loop-starvation.md`). Tasks 1-3 fixed one instance; this is the Task 4
sibling sweep required by `CLAUDE.md`'s "audit recurring fix patterns" rule.

## The bug signature

The session daemon is a single-process, single-event-loop asyncio server (`server/ipc/serve.py` /
`server/app.py`). It multiplexes ~18 background loops (started in `orchestrator.start()`, see
`server/sessions/orchestrator.py` around line 620) plus every HTTP/WebSocket request onto that one
loop. Anything that calls a **synchronous, blocking driver, filesystem, or subprocess API directly
from an `async def` function** — instead of via `asyncio.to_thread` / `loop.run_in_executor` —
stalls the entire daemon for the duration of that call. No other session, no other request, no
other background loop makes progress until it returns.

RC1 was exactly this shape: `server/pipeline/history.py`'s `HistoryStore.__init__` opened a
synchronous `sqlite3.connect(...)` connection, and `server/github/pr_population.py`'s ETA
enrichment queried it directly from a coroutine on the event loop. Every enrichment pass froze the
daemon — no session output, no WebSocket frames, no other automation loop — for as long as the
SQLite read took.

## How RC1 was detected

Results-based monitoring (session dashboards, health checks) didn't show it directly — the daemon
process was alive and answering *eventually*, just with enormous latency. The signal that pinned it
down was a stack sample:

```bash
py-spy dump --pid $(systemctl show -p MainPID --value ccswitch-daemon.service)
```

taken repeatedly (8 samples over 24 seconds). In all 8 samples the **MainThread** frame was sitting
inside `pipeline/history.py`, not inside an `asyncio`/`select`/`epoll` frame. A healthy event-loop
thread should almost always be caught idling in the reactor (`epoll_wait`, `select`, or an asyncio
internal frame) between very short bursts of work; a thread caught synchronously in driver code on
nearly every sample is the signature of a blocking call sitting on the loop rather than a fast task
that runs and yields.

## Why the existing tests missed it

The tests around `HistoryStore` and ETA enrichment asserted **results** — correct durations,
correct percentiles, correct enrichment payloads — never **scheduling latency**. A synchronous
SQLite call that blocks for 200ms still returns the right answer; nothing in a results-only test
distinguishes it from an `await`-friendly call that yields every 10ms. The fix (commits `a8a15ca8`,
`a536c97a`) added `server/tests/test_population_eta_offloads_loop.py`, which runs a concurrent
"ticker" coroutine recording `time.monotonic()` every 10ms while enrichment runs, and asserts the
gaps between ticks stay small. Run the enrichment inline (no `to_thread`) and the ticker freezes for
the full blocking duration; run it correctly offloaded and the ticker keeps ticking. That is the
class of test this pattern needs — asserting the loop stayed *schedulable*, not just that the answer
was correct — and it's what a sibling audit like this one should also demand of any new fix.

A companion structural signal: `server/obs/loop_lag.py` (`f21021cf`, `c2355c0f`) adds a watchdog
that periodically schedules a trivial coroutine and logs a warning if it doesn't run promptly — a
runtime version of the same "is the loop actually being scheduled" question the test asks statically.

## Method for this sweep

1. `grep -rn "sqlite3.connect\|psycopg2.connect\|requests\.\(get\|post\|put\|delete\)\|urllib.request.urlopen" server/ src/ --include=*.py` (excluding tests) — 14 hits.
2. `grep -rn "subprocess.run\|subprocess.check_output\|\.read_text()\|\.write_text()\|os.walk\|\.glob(\|\.rglob(" server/ --include=*.py` (excluding tests) — 55 hits (many are multiple hits in the same function).
3. For every hit: read the containing function, walk its callers with `grep`, and classify:
   - **fixed in P1** — the RC1 site itself.
   - **planned separately** — `server/sessions/replay.py:259` (RC2/P2, per the task brief — not
     investigated further here).
   - **not reachable from the loop** — CLI-only entrypoint (`__main__`/argparse), dead code (no
     callers), startup-only (runs during app/daemon lifespan before `yield`, before traffic is
     served), or already offloaded via `asyncio.to_thread`/`run_in_executor` at every call site.
   - **needs its own issue** — reachable from a coroutine (a background loop or a request handler)
     and genuinely blocking, with no offload.
   - **unresolved** — reachability could not be established with confidence in this pass.

## Full results

### Step 1 — synchronous driver constructors

| Site | What it does | Verdict |
|---|---|---|
| `server/pipeline/history.py:128` | `sqlite3.connect(..., check_same_thread=False)` for `HistoryStore` | **fixed in P1** — now opened for cross-thread use, queried via `asyncio.to_thread` in `pr_population.py`. |
| `server/pipeline/history_migrate.py:32-33` | `sqlite3.connect` on source + dest DB, one-off migration | **not reachable** — `migrate_history()` has zero callers anywhere in `server/`/`src/`; only its sibling `default_dest()` is imported (`server/api/pipeline.py`, `server/github/pr_population.py`). Dead/manually-invoked-only code, no `__main__`. |
| `server/db/__init__.py:36,38` | Comments referencing `sqlite3.connect()` semantics for the async SQLAlchemy engine's `connect_args` | **not reachable** — false positive; no literal `sqlite3.connect` call in this file. The real connection path is `create_async_engine` + `aiosqlite`. |
| `server/github/parity/__main__.py:25` | `urllib.request.urlopen` default for `fetch_oracle` | **not reachable** — `python -m server.github.parity` CLI entrypoint only, not imported by the daemon/app. |
| `server/sessions/push.py:34` (`push()`) | `urllib.request.urlopen` default, POST to webhook | **not reachable** — call wrapped in `await asyncio.to_thread(_post, ...)` (line 42). |
| `server/sessions/push.py:195` (`notify_event()`) | `urllib.request.urlopen` for a user notification channel | **not reachable** — wrapped in `await asyncio.to_thread(_deliver_channel, ...)`. |
| `src/ccswitch/auth_login.py:79` | `sqlite3.connect` to strip Claude/Anthropic cookies from a copied Chrome profile DB | **needs its own issue** — see "Reauth profile prep" below. |
| `server/sessions/copilot_proxy.py:82` (`_default_ready_check`) | `urllib.request.urlopen(timeout=1.0)` polling proxy readiness | **not reachable** — both call sites (`ProxyManager` lines 178, 182) wrap it in `await asyncio.to_thread(self._ready_check, port)`. |
| `server/sessions/copilot_proxy.py:92` (`_default_usage_fetch`) | `urllib.request.urlopen(timeout=2.0)` fetching `/usage` from the copilot proxy | **needs its own issue** — see "Copilot usage fetch" below. |
| `src/ccswitch/oauth.py:175` | `urllib.request.urlopen` — the single choke point for all outbound Anthropic HTTP (refresh/usage) | **not reachable** — `server/` only imports `Usage`/`AuthError` types from this module; the real usage-fetch traffic goes through `ccswitch.accounts.usage_for`, which is only called by `UsagePoller.get()` via `loop.run_in_executor(dedicated_pool, ...)` (see below). |

### Step 2 — filesystem / subprocess

| Site | What it does | Verdict |
|---|---|---|
| `server/library/hooks.py:175` (`set_hooks`) | `path.read_text()` on `settings.json` | **needs its own issue** — see "Library API (hooks/snapshots/scan)" below. |
| `server/library/hooks.py:225` (`dry_run`) | `subprocess.run(["/bin/sh","-c",command], timeout=10)` | **needs its own issue** — same finding; the more severe half (up to 10s block). |
| `server/version.py:30` (`running_git_sha`) | `subprocess.run(["git","rev-parse",...], timeout=5)` | **not reachable in practice** — called directly from `GET /info` and `GET /healthz` (both `async def`), unwrapped, but the function is `@functools.lru_cache(maxsize=1)`: the subprocess only ever executes on the *first* call in the process's lifetime (typically sub-100ms), every subsequent call is a cache hit. Technically a one-time inline blocking call, not a recurring stall — flagged for completeness, not proposed as an issue. |
| `server/reauth.py:468` (`_kill_launcher`) | `pid_file.read_text()` — a single-line PID file | **needs its own issue** (folded into the reauth finding below) — called directly from two `async def` sites (`cancel()`, the timeout handler), unwrapped, but reads a few bytes from a local temp file; negligible duration on its own. Worth fixing alongside the DB/copy work in the same function family since it's the same code path. |
| `server/library/snapshots.py:77` (`_iter_files`, used by `_write_tar_gz`) | `base.rglob("*")` + tars the whole config tree | **needs its own issue** — see "Library API" below. |
| `server/library/snapshots.py:176,178` (`_iter_meta`) | `d.glob("*.json")` + `.read_text()` per snapshot metadata file | **needs its own issue** — same finding. |
| `server/library/scan.py` (`list_plugins`, `plugin_detail`, `list_skills`, `read_skill`, `list_commands`, `read_command`, `list_mcp_servers`, …) | `.read_text()` / `.glob()` / `.rglob()` over the plugin/skill/command tree | **needs its own issue** — see "Library API" below. |
| `server/automations/archive_audit.py:308,343` (`_real_gh`, `_real_gh_num`) | `subprocess.run(["gh", ...])` | **not reachable** — both only ever execute inside `_classify()`, which the loop (`AutomationLoop.tick → _run`) calls as `await asyncio.to_thread(_classify)` (line 106) — one offload wrapping the whole sync pipeline. |
| `server/deploy/git.py` (all functions) | `subprocess.run` for `git` plumbing | **not reachable** — deliberately synchronous helpers; every call site in `server/deploy/controller.py` wraps them individually in `await asyncio.to_thread(self._git.<fn>, ...)` (documented in a comment at controller.py:65). |
| `server/deploy/steps.py` (all functions) | `subprocess.run` for build/restart/health steps | **not reachable** — same pattern; every call goes through `await asyncio.to_thread(self._steps.<fn>, ...)` in `controller.py`. |
| `server/deploy/controller.py:27` (`_changed_files`) | `subprocess.run(["git","diff",...])` | **not reachable** — this sync method is itself invoked via `await asyncio.to_thread(self._changed_files, ...)` (controller.py:113). |
| `server/sessions/titles.py:126` (`read_ai_title`) | `root.glob(f"**/{csid}.jsonl")` — recursive glob over the whole projects tree | **not reachable — dead code.** No callers anywhere in `server/`; `orchestrator.py` imports only the sibling `read_ai_title_from_file` (a direct-path read, no glob), which *is* correctly wrapped in `await asyncio.to_thread(...)` at orchestrator.py:3707. |
| `server/sessions/transcript_repair.py:187` (`find_transcript`) | `glob.glob(config_dir/projects/*/sid.jsonl)` | **not reachable** — the only live entry point, `repair_resumed_transcript`, is called via `await asyncio.to_thread(repair_resumed_transcript, ...)` in `server/sessions/driver.py:542`. |
| `server/sessions/adopt.py:176` (`_resolve_transcript`, used by `discover_candidates`) | `glob.glob(config_dir/projects/*/*.jsonl)` per candidate | **needs its own issue** — see "Adopt-session candidate resolution" below. |
| `server/github/actions_api.py:22` (`_run_gh_bytes`) | `subprocess.run(["gh", ...])` | **not reachable** — module docstring states the intent explicitly ("Sync — the loop wraps them in `asyncio.to_thread`"); confirmed every `CicdDoctorLoop` wiring in `orchestrator.py` (~line 490) passes `lambda ...: asyncio.to_thread(actions_api.<fn>, ...)`. |
| `server/deploy/__main__.py:13` (`_daemon_enabled`) | `subprocess.run(["systemctl","is-enabled",...])` | **not reachable** — `python -m server.deploy` CLI entrypoint, not imported by the running daemon/edge app. |
| `server/auth/identity.py:67` (`_whois_identity`) | `subprocess.run(["tailscale","whois","--json",peer_ip], timeout=2)` | **needs its own issue** — see "Identity verification" below. This is the highest-severity finding in the sweep: it sits on the primary request path, not a background loop. |
| `server/sessions/archive_move.py:59,68` (`_prune_regenerable`, `_tree_size`) | `os.walk` over a session worktree | **not reachable** — both call sites in `orchestrator.py` (`move_into_archive` at line 6429 for archiving, and the `_restore()` closure at line 6754→6766 for unarchiving) are wrapped in `await asyncio.to_thread(...)`. |
| `server/sessions/x11manager.py:475` (`_lock_owner_alive`) | `Path(lock_path).read_text()` — a single-line lock-owner PID | **not reachable** — its only caller, `sweep_stale()`, runs exclusively during app/daemon lifespan startup (`server/app.py:242`, `server/ipc/serve.py:127`), before the app begins serving traffic. |
| `server/sessions/orchestrator.py:5725` (`_find_transcript`, via `transcript_path`) | `glob.glob(projects/*/csid.jsonl)` — fallback after a fast-path `stat()` miss | **needs its own issue — highest priority.** See "Transcript-path glob fallback" below; this is a live near-miss of RC1's exact shape, with a documented prior outage. |

### Step 3 — cross-check against the 17 background loops / request handlers

Confirms the above: `orchestrator.start()` wires `_poller`, `_pr_poller`, `_pr_population`,
`_deploy_watcher`, `_pr_branch_task`, `_rotation_task`, `_titling_task`, `_goal_task`,
`_stall_task`, `_token_refresh_task`, `_backlog_task`, `_autopilot_task`, `_cicd_task`,
`_archaudit_task`, `_sweep_task`, `_archive_limit_task`, `_janitor_task`, `_recurring_task`. Every
loop above whose sync helpers are genuinely reachable from the loop (deploy watcher → `git.py`/
`steps.py`; CI/CD Doctor → `actions_api.py`; archive audit → `archive_audit.py`) turned out to
already route through `asyncio.to_thread` at the loop's own call site. The four "needs its own
issue" findings below reach the loop through *other* paths: three via direct FastAPI/RPC request
handlers, and the fifth (`_kill_launcher`, folded into reauth) via a manually-triggered async
control flow, not a periodic loop.

## Findings that need their own issue (detail)

### 1. Transcript-path glob fallback — `server/sessions/orchestrator.py:5641-5727`

`transcript_path()` (a plain `def`, not `async def`) is called **directly, unwrapped**, from at
least five `async def` methods: `_persist_status` (every status change), `_completion_open_question`
(per-turn completion detection), `_maybe_apply_auto_name` (the titling sweep, one of the 17 loops),
`rehydrate` (daemon startup), and `_snapshot_open_tasks`. It is also called directly, still
unwrapped, from inside `resolve_transcript_path` itself — the `async def` wrapper that three more
call sites (`5272`, `5356`, `5391`) await, believing it to be safe.

Internally, `transcript_path` → `_find_transcript` added a **fast path** (line ~5700): derive the
project directory name from the session's `cwd` and `stat()` one candidate file. The comment at
that line is explicit about why: *"The glob below is O(project dirs) — 1741 on this deployment —
and every entry is a network round-trip when the account store is a remote mount. On rocky that made
a single lookup take >200s, and `_goal_sweep` calls it per session ON THE EVENT LOOP, so the daemon
never finished starting (2026-08-21 outage)."*

That fast path only covers the common case. When it misses — the `cwd`-derived project-dir name
doesn't match (renamed/moved worktrees, archived sessions, edge cases) — execution falls through to
the exact same unbounded `glob.glob(projects/*/csid.jsonl)` that caused the 2026-08-21 outage,
still directly on the event loop, still unwrapped in `asyncio.to_thread`. This is not a hypothetical
sibling; it is the *same* code path that already took the daemon down once, with the mitigation
covering only the majority case rather than closing the hole. Recommend wrapping `_find_transcript`
(or at minimum its glob fallback) in `asyncio.to_thread` the same way `read_ai_title_from_file` and
`repair_resumed_transcript` already are.

### 2. Identity verification — `server/auth/identity.py:53-79`, `server/auth/middleware.py:103,219`

`verify_identity()` is a plain `def`, called synchronously from `IdentityMiddleware.dispatch`
(`async def`, on **every** HTTP request except the health-check exemption list) and from
`require_ws_identity` (every WebSocket upgrade). When the `Tailscale-User-Login` /
`X-Tailscale-User-Login` header is absent, it falls back to `_whois_identity()`, which runs
`subprocess.run(["tailscale", "whois", "--json", peer_ip], timeout=2)` — spawning a process and
blocking for up to 2 seconds, directly on the event loop, per request. In normal operation behind
`tailscale serve` the header is always present and this path is cold, but any request that arrives
without it (misconfigured proxy, direct testing, a client that doesn't go through `tailscale serve`)
stalls the *entire daemon* for up to 2s on the request path — not a background loop, the primary
serving path. This is the most severe of the sibling findings by reach (every request handler, not
one loop) even though its trigger frequency depends on deployment specifics.

### 3. Library API (hooks / snapshots / scan) — `server/api/hooks.py`, `server/api/snapshots.py`, `server/api/library.py`

All three routers declare `async def` path operations and call their `server/library/*` helpers
directly, unwrapped:
- `PUT /api/hooks` → `hooks_lib.set_hooks()` → `path.read_text()`/`path.write_text()` on
  `settings.json`.
- `POST /api/hooks/dry-run` → `hooks_lib.dry_run()` → `subprocess.run(["/bin/sh","-c",command],
  timeout=10)` — up to a **10-second** event-loop stall per dry-run request, the worst single
  blocking window found in this sweep short of a hang.
- `GET/POST /api/snapshots` → `snapshots.list_snapshots()` (globs + reads every snapshot's JSON
  metadata) and `snapshots.create_snapshot()` (`rglob`s and `tar.gz`s the entire config tree).
- `GET /api/library/{plugins,skills,commands,mcp}` and friends → `scan.list_plugins()`,
  `scan.list_skills()`, `scan.read_skill()`, etc. — `rglob`/`glob`/`read_text()` over the plugin,
  skill, and command directory trees on every request.

None of these are in a hot loop, but they are all directly reachable from authenticated API routes,
none are offloaded, and the snapshot/scan paths scale with the size of the user's config directory —
the same "unbounded filesystem scan on the request path" shape as finding #1, just via HTTP instead
of a background sweep.

### 4. Adopt-session candidate resolution — `server/sessions/orchestrator.py:5058-5080`

`list_adoptable_candidates()` (the read-only RPC that lists untracked sessions) correctly wraps
`self._discover_candidates()` in `await asyncio.to_thread(...)` (line 4970). But `adopt_session()`
— the RPC that actually performs an adoption — calls `self._resolve_candidate(claude_session_id)`
**directly, unwrapped**, which re-runs the identical `_adopt.discover_candidates()` (process
enumeration over `/proc` plus, per mapped account, a `glob.glob(projects/*/*.jsonl)` scan) on the
event loop. Same underlying scan as the already-offloaded sibling three lines away — a straight
inconsistency, not a design choice.

### 5. Copilot usage fetch — `server/sessions/orchestrator.py:4787-4789`, `server/sessions/copilot_proxy.py:88-93,227-234`

`CopilotProxyManager.usage()` (plain `def`) calls `self._usage_fetch(spec.port)` directly —
`urllib.request.urlopen(timeout=2.0)` against the local copilot2api proxy — with no `to_thread`
wrapper, unlike its sibling `ready_check`, which *is* offloaded two call sites above it in the same
file. It's reached via `orchestrator.copilot_usage()` (`async def`), which is registered as an RPC
method (`server/ipc/orchestrator_rpc.py:279`) and has a `RemoteDriver` client wrapper
(`server/sessions/remote.py:694-696`), so the RPC surface is live even though no current UI code
path was found calling it. Copilot-only (opt-in feature) and bounded to 2s, so lower severity than
findings #1-3, but the same fix (`asyncio.to_thread`) closes it.

### 6. Reauth profile preparation — `src/ccswitch/auth_login.py:79`, `server/reauth.py:171,203,468`

`ReauthManager`'s production runner (`server/reauth.py:145`, `async def _production_runner`) is
awaited directly on the event loop (`await asyncio.wait_for(self._runner(name), ...)` at line 364).
Inside it, `auth_login.prepare_reauth_profile()` (line 171) and `auth_login.cleanup_reauth_profile()`
(line 203) are called **unwrapped**, sitting between two `asyncio.to_thread` calls
(`capture_login` at 192-193, `mark_reauthenticated` at 199-200) that show the pattern was
deliberately applied elsewhere in the same function but missed here. `prepare_reauth_profile`
copies the Chrome `Cookies`/`Local State` files (`shutil.copy`) and opens a synchronous
`sqlite3.connect()` to `DELETE` Claude/Anthropic cookie rows and `commit()` — the same "sync sqlite3
write reachable from a coroutine" shape as RC1, just triggered by a manual reauth flow instead of a
periodic poll. `_kill_launcher()` (reauth.py:468, called from two more unwrapped `async def` sites)
adds a `pid_file.read_text()` in the same family — negligible in isolation, but part of the same
code path and worth fixing together.

## Unresolved

None. Every hit from Steps 1-2 was traced to a definite verdict; no site needed to be left as
"unresolved — needs manual check."

## Issue candidates (NOT filed)

Per instruction, no GitHub issues were opened for this audit. The following are proposed for the
user's review and filing decision, ordered by proposed priority.

---

**1. Transcript-path glob fallback still blocks the event loop after a fast-path miss**

`server/sessions/orchestrator.py`'s `_find_transcript`/`transcript_path` added a `stat()` fast path
(commit around `2026-08-21`) after an unbounded `glob.glob` over 1741 project directories took the
daemon down for hours on `rocky`. The fast path only covers the common case; on a miss, the exact
same glob still runs directly on the event loop, unwrapped in `asyncio.to_thread`, from at least
eight call sites including the titling sweep and `resolve_transcript_path`'s own "safe" wrapper.
This is a live recurrence of the outage that motivated this whole P1 plan, not a new pattern. Fix:
wrap `_find_transcript`'s fallback path (or the whole function) in `asyncio.to_thread` at its
callers, the same way `read_ai_title_from_file` and `repair_resumed_transcript` already are.

**2. `verify_identity`'s tailscale-whois fallback blocks every request for up to 2s on a header miss**

`server/auth/identity.py:_whois_identity` runs `subprocess.run(["tailscale","whois",...],
timeout=2)` synchronously inside `IdentityMiddleware.dispatch` (`async def`, on the request path
for every HTTP request and WS upgrade) whenever the `Tailscale-User-Login` header is absent. Unlike
the other findings this sits on the primary serving path rather than a background loop, so any
client that reaches the daemon without that header stalls the whole process for up to 2 seconds per
request. Fix: wrap the `subprocess.run` call in `asyncio.to_thread`.

**3. Library API routes (hooks dry-run, snapshots, plugin/skill/command scan) block the loop on every request**

`server/api/hooks.py`, `server/api/snapshots.py`, and `server/api/library.py` all call their
`server/library/*` helpers directly from `async def` route handlers with no `asyncio.to_thread`
wrapper. The worst is `POST /api/hooks/dry-run`, which can block the loop up to its own 10-second
timeout running a user-supplied shell command; `POST /api/snapshots` `rglob`s and tars the entire
config tree; `GET /api/library/*` `rglob`s/reads every plugin, skill, and command file on every
request. None of these are in a background loop, but all are reachable from authenticated,
regularly-used API routes and scale with the user's config directory size. Fix: wrap each library
call in `asyncio.to_thread` at its route handler (or move the offload into the library functions
themselves, consistent with `deploy/steps.py`'s pattern).

**4. `adopt_session` blocks the loop on a filesystem/process scan that its sibling RPC already offloads**

`server/sessions/orchestrator.py:adopt_session` calls `self._resolve_candidate(...)` directly, which
re-runs `_adopt.discover_candidates()` (process enumeration + a `glob.glob` per mapped account) on
the event loop — while the read-only sibling `list_adoptable_candidates()` three lines above
correctly wraps the identical scan in `asyncio.to_thread`. Fix: wrap the `_resolve_candidate` call
in `adopt_session` the same way.

**5. Copilot `/usage` fetch and reauth profile prep block the loop on their respective (lower-traffic) paths**

Two smaller, related findings: `CopilotProxyManager.usage()` (`server/sessions/copilot_proxy.py`)
calls a synchronous `urllib.request.urlopen(timeout=2.0)` directly from the `copilot_usage` RPC path,
unlike its sibling `ready_check`, which is offloaded two lines above it. Separately,
`ReauthManager`'s production runner (`server/reauth.py`) calls `auth_login.prepare_reauth_profile`/
`cleanup_reauth_profile` (a `sqlite3.connect` + `DELETE` + `commit`, plus file copies) and
`_kill_launcher` (a small `read_text()`) unwrapped, sandwiched between two calls in the same
function that already use `asyncio.to_thread` correctly. Both are opt-in/low-frequency features
(Copilot accounts, manual reauth) so lower priority than 1-4, but the same one-line fix applies to
each. Fix: wrap each in `asyncio.to_thread`, consistent with the sibling calls already correct in
the same functions.
