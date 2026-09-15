# Cross-host session transfer: move a session between fleet hosts

**Date:** 2026-09-14
**Status:** draft — awaiting Tony's review. Four decisions are called out under
"Decisions to confirm"; everything else follows from the code as it exists today.
**Area:** `server/sessions/` (orchestrator, fleet_orchestrator, fleet, remote, a new
`transfer.py`), `server/ipc/orchestrator_rpc.py`, `server/api/sessions.py`,
`server/api/fleet.py`, `server/db/`, `src/ccswitch/` (a new `worktree_transfer.py`),
`web/src/features/sessions/`, `web/src/features/settings/sections/FleetSection.tsx`.
**Related:** #949 (spec 002 Phase 4: adoption off dead hosts), #1432 (recovery resumed a
foreign-host session and stranded UI routing), #1379 (per-session lease), #852 (fleet).

## Problem

The fleet places a session on a host once, at creation, and never again. Placement is
scored (`server/sessions/fleet.py:score_host` weighs headroom, memory, and load) but a
session created on a quiet host stays there after the host fills up. Measured at
15:47 local on 2026-09-14:

| host | cores | load (1 min) | free memory | sessions |
|---|---|---|---|---|
| dobby | 16 | 37.3 | 1 GB | 8 |
| rocky | 8 | 2.7 | 26 GB | 3 |

Nothing in the product can move any of dobby's eight sessions to rocky. The only path is
the hand procedure recorded in #1432: stop the session, edit `sessions.host` in the
database, resume on the other host, and restart the edge to flush its route cache. That
procedure proved the critical fact this design rests on: a Claude conversation resumed on
the other host with the same native conversation id, and live WebSocket replay and fresh
tool output worked. This spec turns that procedure into a product operation, then drives
it from a policy so the fleet balances itself.

## What already exists (route through, do not duplicate)

- **Host identity and routing.** `sessions.host` (`server/db/models.py:199`) is the single
  routing stamp. `FleetOrchestrator._host_for` (`fleet_orchestrator.py:189`) reads it
  through a per-process cache that is never invalidated; per-session RPCs are dispatched
  by `_make_per_session` (`:210`). A worker daemon rehydrates only rows stamped with its
  own name (`orchestrator.py:11363`); the primary loads every row and gates acting on
  `_owns_record` (`:15198`).
- **Single-owner lease.** `server/sessions/session_lease.py` makes "at most one live CLI
  per session" a database fact: holder is the host name, `release` marks the row
  epoch-stale so a different holder's `acquire` succeeds immediately with a bumped fence.
- **Driver swap primitive.** `_reassign_locked` (`orchestrator.py:16192`) is the one
  sanctioned way to close a driver and rebuild it with `resume=<engine_session_id>`:
  bounded interrupt, `aclose`, re-claim lease, rebuild overlay, `start`, then persist. It
  serializes on `self._swapping`, which `ensure_driver` and every attach already respect.
- **Resume inputs are already shared.** `~/.claude` and `~/.claude-accounts` are canonical
  on dobby and mounted over sshfs on every runner (`scratch_config.py:6`). The transcript
  for a session lives at `~/.claude/projects/<encoded-cwd>/<engine_session_id>.jsonl` on
  that shared tree; `transcript_path` (`orchestrator.py:14947`) resolves it by globbing the
  globally unique id. Both hosts keep worktrees at the same path,
  `<repo_root>/.ccswitch/worktrees/<repo>/<short-id>` (verified on dobby and rocky), so a
  moved session's `cwd` and encoded transcript directory are byte-identical on the target.
- **Worktree lifecycle.** `src/ccswitch/worktrees.py:create_worktree` (`:338`) builds the
  per-session worktree, `remove_worktree` (`:453`) tears it down, and `authored_branches`
  (`:236`) already knows which branches a session has checked out.
- **Placement scoring.** `fleet.HostCandidate`, `_dispatchable`, `score_host`, `rank_hosts`
  (`fleet.py:39-126`) and `FleetRouter.pick` (`fleet_router.py:155`). The rebalancer reuses
  these; it does not grow a second scoring function.
- **RPC plumbing.** A new daemon method is one entry in `_SIMPLE`
  (`server/ipc/orchestrator_rpc.py:314`) plus a mirror on `RemoteOrchestrator`
  (`server/sessions/remote.py`).
- **Per-session action menu and host picker.** `SessionActions.tsx` renders opt-in menu
  items (Reload, Next best account); `DispatchBar.tsx` already has an engine-filtered
  Runner `<select>` backed by `useFleetHosts` and `hostDispatchable`.
- **Audit.** `server/audit.py:record_audit`.

## Decisions to confirm

These four choices shape the feature. The spec is written as if they hold; override any of
them and the affected sections are marked.

1. **Only at a turn boundary.** A session is movable only when no turn is in flight
   (`status` not in `MID_TURN_STATUSES`). There is no "interrupt and move": a user who
   wants to move a working session presses the existing Interrupt first. The rebalancer
   simply waits. This keeps the transfer free of prompt-replay semantics.
2. **Uncommitted work moves only on explicit consent, never automatically.** This is the
   constraint #1432 recorded ("do not automatically migrate uncommitted work"). A manual
   move of a dirty worktree requires ticking "Carry uncommitted changes" in the dialog;
   the rebalancer only ever moves sessions whose worktree is clean.
3. **Worktree contents travel through git, not rsync.** The source pushes the session's
   HEAD and its unpushed authored branches to a hidden ref namespace on `origin`
   (`refs/viberpit/transfer/<session>/…`), the target fetches them and re-creates the
   worktree at the same path. No host-to-host ssh trust is added, no second transport
   exists, and the daemons keep verifying only the edge. Ignored files (`node_modules`
   symlinks, build output) do not travel; they are rebuilt, as any fresh worktree does.
4. **The primitive lands first, the policy second, with the policy off by default.**
   Phase 1 is the manual "Move to host" and "Drain host" operations. Phase 2 is the
   rebalance loop behind `VIBERPIT_REBALANCE_ENABLED=false`. Turning it on is a deploy
   decision, not a code change.

## Design

### Vocabulary

- **Source**: the daemon currently holding the session's lease and driver.
- **Target**: the daemon that will hold them after the move.
- **Manifest**: the JSON dict the source returns from export and the target consumes in
  import. It is the only state that crosses hosts by way of the edge.
- **Transfer refs**: `refs/viberpit/transfer/<session-id>/head` and
  `refs/viberpit/transfer/<session-id>/branches/<name>` on `origin`. Hidden from branch
  listings, never trigger CI or PR automation, deleted after import.

### What moves and what does not

| State | Where it lives | Moves how |
|---|---|---|
| DB row (`sessions`) | shared Postgres | `host` restamped by the edge |
| Transcript JSONL | shared `~/.claude/projects` | nothing to do; same path on the target |
| Account config dir | shared `~/.claude-accounts` | nothing to do |
| Per-session lease | `daemon_leases` | source releases, target acquires |
| Worktree checkout | host-local disk | transfer refs on `origin`, re-created at the same path |
| Uncommitted edits | host-local disk | WIP commit inside the transfer ref, unpacked on the target (consent required) |
| Unpushed authored branches | host-local clone | transfer refs; recreated on the target clone if the name is free |
| Daemon record, driver, ring buffer, `_last_prompt` | source process memory | driver rebuilt on the target; ring refilled from the transcript; nothing else needed at a turn boundary |
| Codex `CODEX_HOME`, app-server history | host-local, by design | does not move; Codex sessions are not transferable in v1 |
| Non-repo scratch directory | host-local | does not move; non-repo sessions are not transferable in v1 |

### Movable predicate

`transfer.movable(record, hosts) -> Verdict` is a pure function so the API, the dialog,
and the rebalancer agree. A session is movable to `target` iff every line holds:

- `engine` declares `AgentCapabilities.cross_host_transfer` (Claude: true; Codex: false,
  because its history is inside a host-local app-server).
- `kind` is not an assistant (assistants are pinned to the primary at creation).
- `display_mode` is virtual. A native-display session is bound to the browser's node and
  is refused with `native_display`.
- `status` is not in `MID_TURN_STATUSES` and not in `INACTIVE_SESSION_STATUSES`.
- `moving_to` is null (no transfer in flight).
- the session has a repo worktree (`cwd` under the managed worktree root, per
  `worktrees.is_managed_path`).
- `target` differs from the current host, is `enabled`, `healthy is True`, passes
  `engine_rejection` for the session's engine, and is under `max_sessions`.

A queued session (no driver yet, no worktree yet) is movable by restamp alone: set
`host`, then call the target's `maybe_start_queued`. The saga below is for sessions that
have a worktree.

### The transfer saga

The edge drives it; daemons never talk to each other. Implemented in
`server/sessions/transfer.py` as `async def transfer(sessions, session_id, target, *,
carry_dirty, initiator, reason) -> TransferOutcome`, where `sessions` is the
`FleetOrchestrator`. Outcomes: `moved`, `restored` (target failed, session back on the
source), `failed` (both failed, session parked), plus the refusal codes from the predicate.

1. **Claim the move.** One conditional `UPDATE sessions SET moving_to=:t WHERE id=:id AND
   moving_to IS NULL`. Zero rows updated means another move is in flight: refuse with
   `transfer_in_flight`. Everything after this runs under a `try/finally` that clears
   `moving_to`.
2. **Export on the source** (`transfer_export(session_id, carry_dirty)` RPC, routed to
   `sessions.host`). Inside the orchestrator, under the `_swapping` slot for the session:
   - re-check the turn boundary; refuse `mid_turn` if a prompt landed in between;
   - `worktree_transfer.inspect(cwd)`: dirty? if so and not `carry_dirty`, refuse
     `dirty_worktree` with the file count;
   - close the driver the way `_reassign_locked` does: bounded interrupt, `aclose`;
   - `worktree_transfer.export(cwd, session_id)`: optional WIP commit, push transfer refs,
     return the bundle description;
   - release the lease, drop the record from `_records`, `_drivers`, `_worktrees`. The
     worktree directory stays on disk until finalize.
   - return the manifest: `engine_session_id`, `cwd`, `branch`, `head_sha`, `wip_commit`,
     `branches {name: sha}`, `permission_mode`, `model`, `status`.
   If the push fails after the driver closed, the source re-runs its own import path
   (below) from the same manifest so the session comes back on the source, and export
   returns `export_failed`. The edge clears `moving_to` and stops.
3. **Restamp.** `UPDATE sessions SET host=:target, moved_from=:source, moved_at=<db_utc_now()>`.
   `FleetOrchestrator.note_host(session_id, target)` updates the route cache. Audit
   `session.transfer` with `{from, to, initiator, reason, carry_dirty}`.
4. **Import on the target** (`transfer_import(session_id, manifest)` RPC, routed
   explicitly to `target`, not through `_host_for`):
   - `_claim_session` (acquire the lease; the source released it, so this succeeds at
     once with a bumped fence);
   - `_adopt_row(row)`: the per-row normalization extracted from `rehydrate` (the same
     extraction #949 Task 18 calls for), so a transferred row is loaded exactly the way
     a restart loads it. On the primary the row may already exist as a dashboard mirror;
     adopt refreshes it rather than failing on "already known";
   - `worktree_transfer.import_(manifest, repo_root)`: fetch the transfer refs, create
     the worktree at the canonical path on the manifest branch (or detached at
     `head_sha`), unpack the WIP commit with `git reset --mixed HEAD~1` so uncommitted
     work arrives as unstaged edits, recreate transferred branches whose names are free,
     lock the worktree. A worktree already present at the path with the expected HEAD
     (the restore-to-source case) is reused. If the manifest branch is checked out by
     another worktree on the target clone, refuse `branch_busy_on_target`: the child
     branch fallback in `create_worktree` would silently rename the agent's branch;
   - `ensure_driver` with `resume=engine_session_id`, then queue the moved-host nudge
     (below) as the next user message the way `RESUME_NUDGE` is delivered;
   - delete the transfer refs on `origin`.
   If import fails: restamp `host` back to the source, call the **source's**
   `transfer_import` with the same manifest (its worktree is still on disk, so this is a
   reuse, not a rebuild), and return `restored`. If that fails too, persist
   `status=needs_attention`, `paused_reason="transfer_failed"`, `host=source`, and return
   `failed`; the row is intact and the source daemon rehydrates it on its next restart or
   on a manual Reload.
5. **Finalize on the source** (`transfer_finalize(session_id, manifest)` RPC): reset the
   local branch to its pre-WIP tip if a WIP commit was made, `remove_worktree(force=True)`.
   Retried three times with backoff inside the request; a final failure is logged and
   audited as `session.transfer.finalize_failed` but does not change the outcome, because
   the session is already live on the target. A leftover locked worktree on the source is
   a disk leak, not a correctness problem.

Wall-clock budget: the API call is synchronous and returns the outcome, like Reload and
Rotate. Expected 5–30 s (driver close, one push, one fetch, one resume). Hard budget 120 s
via `rpc_deadline`.

### Worktree carry-over (`src/ccswitch/worktree_transfer.py`)

Pure git plumbing, no orchestrator imports, tested against a temp repo with a bare origin.

- `inspect(cwd) -> WorktreeState`: `dirty` (from `git status --porcelain`, ignored files
  excluded), `branch` (None when detached), `head_sha`, `unpushed` (authored branches whose
  tip is not on `origin` at the same sha).
- `export(cwd, session_id, *, carry_dirty) -> Bundle`: if dirty, `git add -A` and commit
  `viberpit: transfer WIP for session <id>; not for merge` with `--no-verify` (hooks are
  the agent's business, not the mover's). Push `+HEAD:refs/viberpit/transfer/<id>/head`
  and `+refs/heads/<b>:refs/viberpit/transfer/<id>/branches/<b>` for each unpushed
  authored branch. Clean-and-pushed is the fast path: no WIP commit, still one push of
  `head` so the target never depends on the source clone.
- `import_(bundle, repo_root) -> WorktreeInfo`: as in step 4. Recreated branches use
  `git branch -f` only when the name is absent on the target clone; an existing local
  branch of the same name at a different sha is left alone and reported in the outcome.
- `finalize(cwd, bundle)`: as in step 5.
- `delete_transfer_refs(repo_top, session_id)`: `git push origin --delete` of the
  namespace. A sweep of stale `refs/viberpit/transfer/*` older than a day is a follow-up
  if leaks are observed; they are invisible to users and CI meanwhile.

### Routing and attach while a move is in flight

- `FleetOrchestrator._host_for` re-reads the row when the cache misses **and** when a
  cached host answers "no session with id"; on that answer it drops the cache entry,
  re-reads, and retries once. This also closes the stale-route failure in #1432 without
  an edge restart.
- While `moving_to` is set, every per-session RPC that would build or use a driver is
  refused with `transfer_in_flight` (HTTP 409). Reads that only touch the DB row
  (`get_session`, list) keep working and show `movingTo`.
- The bridge (`server/bridge/ws.py`) maps that refusal to an error frame with code
  `SESSION_MOVING` and `retryAfterMs`. The client shows "Moving to rocky…" in place of the
  stream and re-attaches on a 2 s backoff. A browser attached to the source sees its live
  stream end when the driver closes, gap-heals into a re-attach, receives `SESSION_MOVING`
  until step 4 completes, and then attaches to the target with a full replay served from
  the shared transcript. Reusing the existing gap-heal path means no new client state
  machine.
- `_owns_record` on the primary and the worker's rehydrate partition are unchanged: both
  already key on `sessions.host`, which is the single stamp the saga rewrites.

### Moved-host nudge

Delivered to the agent on the target as the next user message, mirroring the strings spec
002 Task 19 specifies:

> This session was moved from **dobby** to **rocky** at 2026-09-14 15:52 EDT. Your worktree
> was re-created at the same path. Uncommitted changes were carried over as unstaged edits.
> Run `git status` before continuing. Anything left unpushed on dobby that was not on your
> current branch or a branch you checked out here may not be present.

The second and third sentences vary with `wip_commit` and the recreated-branch report.

### Daemon RPC surface

Three `_SIMPLE` entries and `RemoteOrchestrator` mirrors: `transfer_export`,
`transfer_import`, `transfer_finalize`. They are **not** added to `_PER_SESSION`; the saga
routes each explicitly through `router.get(host)` because export and import go to
different hosts for the same session id. All three take and return JSON-scalar dicts.

### Edge API

- `POST /api/sessions/{id}/transfer` body `{host, carryDirty?: bool, reason?: string}` →
  200 `{sessionId, from, to, outcome}`. Errors: 400 `unknown_host` / `same_host`; 409
  `mid_turn`, `dirty_worktree` (with `{files}`), `transfer_in_flight`, `not_transferable`
  (with the predicate's reason: `engine`, `assistant`, `native_display`, `non_repo`,
  `branch_busy_on_target`); 503 `no_engine_host_available` reusing the existing
  `hostReasons` shape. Audited with `via=api`.
- `POST /api/sessions/batch/transfer` `{ids, host, carryDirty?}` → per-id outcomes,
  the shape `batch/rotate` uses. Runs moves sequentially; two concurrent pushes from one
  clone would contend on the clone lock anyway.
- `POST /api/fleet/hosts/{name}/drain` `{to?: host}` → moves every movable, clean session
  off `name`, target chosen per session by `FleetRouter.pick` excluding `name` (or the
  explicit `to`). Returns the per-session outcomes and the list of sessions left behind
  with their refusal reasons. This is the maintenance operation the rocky runbook's
  "rollback to dobby" section does by hand today.
- `GET /api/sessions/{id}` and the list gain `movingTo`, `movedFrom`, `movedAt`,
  `hostPinned`.
- `PATCH /api/sessions/{id}` accepts `hostPinned`.

### Schema

Four nullable columns on `sessions`, picked up by reconcile on both dialects with no
`_REQUIRED_EXTRAS` entry: `moving_to` (str), `moved_from` (str), `moved_at` (datetime,
naive UTC via `db_utc_now`), `host_pinned` (bool). One nullable column on `hosts`: `cpus`
(int), reported by `/info` and written by `probe_host`, so load can be normalized per core.
One alembic revision chained off the newest, as the record of the change.

### Engine capability

`AgentCapabilities.cross_host_transfer: bool = False`, set true on the Claude engine only.
The doc comment names the two prerequisites: conversation history on the shared tree and
resume by conversation id. Codex fails the first (`CODEX_HOME` is host-local by
`assert_local_filesystem`).

### UI (Phase 1)

- `SessionActions.tsx` gains an opt-in `onMoveHost` item, "Move to host…", enabled only
  when `movable` (the API's predicate is exposed on the record as `transferable:
  {ok, reason}` so the menu does not re-derive it).
- The dialog lives in `SessionHeader` per the existing convention: a Runner select built
  from `useEnabledFleetHosts()` filtered by `hostDispatchable(h, engine)` minus the current
  host; a worktree line ("Clean, everything pushed" or "3 uncommitted files") with a
  "Carry uncommitted changes" checkbox that is unchecked by default and required to
  proceed when dirty; a Move button that stays disabled mid-turn with "Wait for the turn to
  finish or interrupt first".
- `SessionCard` line 2 shows "moving to rocky…" while `movingTo` is set, using the
  existing `card-host` slot.
- `FleetSection.tsx` gets a per-host "Drain sessions…" action with a confirmation that
  lists what will move and what will stay.

### Phase 2: rebalance policy

An edge loop next to `probe_loop`, `rebalance_loop(interval_s)`, gated by
`VIBERPIT_REBALANCE_ENABLED` (default false) and by the primary lease so only one edge acts.
Each tick:

1. Read `hosts`. A host is **overloaded** when `load_avg / cpus >
   VIBERPIT_REBALANCE_LOAD_PER_CPU` (default 1.5) or `mem_available_mb <
   VIBERPIT_REBALANCE_MIN_MEM_MB` (default 2048). A host is a **receiver** when it is
   dispatchable, `load_avg / cpus < 0.5 ×` the threshold, and memory is above `2 ×` the
   minimum. The gap between the two thresholds is the hysteresis that prevents ping-pong.
2. With at least one of each, pick the overloaded host with the worst `score_host` and the
   receiver with the best.
3. Candidates on the overloaded host: `movable` to the receiver, `carry_dirty=False`, not
   `host_pinned`, `moved_at` older than `VIBERPIT_REBALANCE_COOLDOWN_S` (default 3600).
   Order by last activity, oldest first: an idle session's CLI is pure memory cost on the
   busy host and its move cannot interrupt anyone.
4. Move **one** session, with `initiator=rebalance`, then stop. The next tick re-probes
   before deciding again. `VIBERPIT_REBALANCE_MAX_MOVES_PER_HOUR` (default 4) caps the
   global rate.

Act-then-notify, per the standing principle: the move is audited, the session receives the
nudge, and the Fleet page lists the last moves. Nothing waits on a human. Per-session
opt-out is `hostPinned`; there is no per-host opt-out beyond `enabled` and `max_sessions`
in v1.

Today's numbers would put dobby at 2.3 load per core and rocky at 0.34, so the loop would
move one clean idle session per tick until dobby drops under 1.5 per core or rocky rises
above 0.75.

### Relationship to spec 002 Phase 4 (#949) and #1432

- **Adoption is a transfer whose source is dead.** #949's Task 18 should call the same
  `transfer_import` with a manifest built from the DB row alone (`engine_session_id`,
  `cwd`, `branch`, no transfer refs), after `declare_dead` fences the source. The
  `_adopt_row` extraction this spec makes is the "shared rehydrate normalization
  (extracted, not copied)" that Task 18 asks for. #949 keeps its own trigger, fencing,
  and the fresh-spawn fallback for a missing transcript; it does not grow a second
  import path.
- **#1432's route-cache and attach-error items** are addressed here: cache self-heal on
  "no session", `SESSION_MOVING` instead of `SESSION_NOT_FOUND` during a move. Its
  ownership audit of `rearm_background_tasks` and `_resume_eligible` stays with #1432.
- **002's out-of-scope line** ("cross-host migration of live worktree contents") is
  narrowed, not contradicted: contents move through git, on consent, and never for a
  dead source.

### Security

- No new trust edges. Daemons still accept only the edge's client certificate; the edge
  still calls daemons. Git traffic goes to `origin` with the credentials each host already
  uses to open PRs.
- The transfer ref namespace is under the repo's normal push permissions. A WIP commit
  contains whatever the worktree contained; it is deleted from `origin` after import, and
  the same bytes would have been pushed on the agent's next commit anyway. Session consent
  (the checkbox) is the gate for dirty trees; the rebalancer never pushes uncommitted work.
- Kill switch `VIBERPIT_TRANSFER_ENABLED` (default true) disables the API and the loop
  together. New settings take the `VIBERPIT_` prefix.

### Verification gate before Phase 1 code (Principle III)

#1432 already demonstrated resume-by-conversation-id on the other host at the same path.
Phase 1 still opens with `scripts/verify-cross-host-resume.sh` (the drill 002 Task 17
specified and never ran) so the claim is reproducible: on rocky, re-create a worktree for
an **archived** dobby session at its canonical path and run `claude -p --resume <id>`
against the composed config dir; the conversation must continue. An archived session is
used so the drill can never create a twin of a live CLI.

### Testing

- `server/tests/test_transfer_saga.py`: the saga against two fake orchestrators sharing a
  SQLite DB. One test per failure point (refused export, push failure after close, import
  failure with successful restore, import and restore both failing, finalize failure).
  Each asserts the terminal `host`, `moving_to`, lease holder and fence, and the audit row.
- `tests/test_worktree_transfer.py` (core suite, run with `PYTHONPATH=$PWD/src`): temp
  repo plus bare origin; cases: clean and pushed, dirty with consent, dirty without consent
  refused, detached HEAD, unpushed authored branch, branch name busy on the target, WIP
  unpacked as unstaged edits with the branch tip unchanged, transfer refs deleted after
  import.
- Orchestrator: `_adopt_row` extraction keeps every existing rehydrate test green;
  `transfer_export` refuses under `MID_TURN_STATUSES`; `transfer_import` on the primary
  refreshes an existing mirror record.
- Fleet: `_host_for` self-heal on "no session"; per-session RPC 409 while `moving_to` is
  set; `movable` predicate table-driven over engine, kind, display, status, target state.
- API: status codes above; batch shape; drain leaves-behind report.
- Rebalance: pure policy function over `HostCandidate` lists (overloaded/receiver
  selection, hysteresis, cooldown, pin, rate cap) with no I/O; loop test that it moves
  exactly one session per tick.
- Web: `SessionActions` item gating, dialog consent rule, card chip, Fleet drain
  confirmation. Full `npm run test`, since `SessionList` consumers mock `@/api/hooks`.
- Postgres suite: the new columns are nullable, so no list entries; run it anyway because
  `moved_at` is a datetime column.

### Out of scope

- Moving Codex sessions (host-local history) and non-repo sessions (host-local scratch
  dir). Both are refused with a reason, not silently skipped.
- Interrupt-and-move. Use Interrupt, then Move.
- Moving ignored files, virtual environments, or `node_modules`.
- Cross-host adoption of a dead host's sessions (#949) beyond sharing `transfer_import`.
- A second edge process. The route cache is per process; the self-heal makes a stale
  entry survivable, not impossible.

### Phasing

1. **PR 1a, core:** `src/ccswitch/worktree_transfer.py` plus tests.
2. **PR 1b, server:** columns and revision, capability, `_adopt_row` extraction, the three
   RPCs, `transfer.py` saga, `movable`, API endpoints, route-cache self-heal, bridge
   `SESSION_MOVING`, audit, the verification drill script.
3. **PR 2, web:** Move dialog, card chip, batch, Fleet drain.
4. **PR 3, policy:** `cpus` in `/info` and `hosts`, `rebalance_loop`, settings, Fleet page
   "recent moves". Ships with the loop disabled.
5. Enable `VIBERPIT_REBALANCE_ENABLED` on dobby's edge after a week of manual moves have
   produced no `restored` or `failed` outcomes.
