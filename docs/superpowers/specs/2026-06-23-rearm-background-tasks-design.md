# Re-arm background tasks across daemon redeploys — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorming) — pending implementation plan
**Topic:** When a deploy restarts the daemon, automatically relaunch the background tasks
(harness Monitors and backgrounded Task agents) that were live in each session, so they survive
redeploys instead of dying silently.

## Problem

Background tasks launched inside a `claude` session — the harness **Monitor** watcher and any
backgrounded **Task** agent — run *inside* the `claude` subprocess. A deploy restarts the daemon,
and the daemon unit's `KillMode=control-group` (`deploy/ccswitch-daemon.service:16`) SIGTERMs the
entire cgroup, killing every session subprocess **and** the in-process background tasks they spawned.

The graceful-drain handshake (`#190`, `orchestrator.begin_drain` / `drain_status`,
`server/sessions/orchestrator.py`) protects only **foreground turns**: `active_session_count()`
counts sessions whose status is in `_ACTIVE_STATUSES = {in_progress, working, waiting_input}`. A
backgrounded task does **not** flip any of those — once the foreground turn ends the session reports
`done`/`idle`, so drain declares `ready`, the deploy proceeds, and the task is reaped.

Observed symptom: a `Monitor "PR #10604 CI/merge status"` ran for 7+ minutes, then emitted
`<status>killed</status>` the moment a deploy landed. The session itself resumes fine from the
transcript; the **monitor is gone for good** — rehydrate replays the conversation but has no concept
of background-task state, so nothing brings it back.

### Why deferral doesn't solve it

The drain cap is 180s (`CCSWITCH_DEPLOY_DRAIN_CAP_SEC`). Monitors routinely outlive any reasonable
drain window (the observed one was still going at 7 min). "Defer the deploy until the task finishes"
would, for the long-lived tasks we actually care about, only delay the kill by ≤3 min and then kill
it anyway — while directly fighting `#190`'s near-real-time-deploy goal. Preserving the task across
the restart is the only outcome that helps.

### What the daemon can and can't observe

(Confirmed by code survey.)

- Session status is a **pure turn-level state machine** (`server/sessions/status.py:24`,
  `derive_status`) over signals snapshotted from the live driver (`server/sessions/driver.py`,
  `_dash_signals`): `turn_active`, `pending_permission`, `pending_question`, `pending_plan`,
  `turns_completed`, `last_turn_outcome`. **No background-task awareness anywhere.**
- The translator (`server/sessions/translator.py`) handles `SystemMessage` / `StreamEvent` /
  `AssistantMessage` / `UserMessage` / `ResultMessage` / `RateLimitEvent` — there is no dedicated
  background-task lifecycle event from the SDK.
- **But** the background-task blocks *do* flow through the daemon's message pump — that is how the
  `<task-notification>` blocks reached the browser. So the launching `tool_use` block and the
  `<task-notification>` blocks are observable in the message stream the daemon already relays. That
  stream is the only viable capture point; there is no out-of-band task registry the daemon can read.
- `rehydrate()` (`server/sessions/orchestrator.py`, ~`:647`) is **lazy** — it rebuilds
  `SessionRecord`s from the DB and normalizes interrupted `working`/`in_progress` to `idle`; drivers
  are spawned on first attach (`ensure_driver`, `resume=<claude_session_id>`). There is no hook today
  that knows a session had live background work.

## Goals

- A background task that was live when a deploy restarted the daemon is **automatically relaunched**
  in the resumed session, without the user doing anything.
- Relaunch is **faithful** (same parameters as the original launch) and **safe** for side-effectful
  tasks (no blind double-application of work).
- When auto-relaunch can't run, the loss is **visible and recoverable**, never silent.
- No change to the harness; degrade gracefully if the harness block shapes drift.

## Non-goals

- Mid-flight *resumption* of a task's internal progress. The harness in-process state is destroyed on
  kill; "re-arm" means **relaunch from the original prompt**, not resume from the interruption point.
- Protecting tasks during non-deploy crashes as a designed feature (the same persistence covers it
  incidentally, but it is not a goal and is not tested as such).
- Making `drain` defer for background tasks. We chose re-arm over deferral (see "Why deferral doesn't
  solve it").

## Decisions (from brainstorming)

1. **Strategy: re-arm after restart** — relaunch killed background tasks, not defer-and-kill.
2. **Timing: auto on restart** — relaunch runs unattended for every session that had live tasks, the
   moment the daemon comes back; we do not wait for the user to return.
3. **Scope: all tasks auto** — Monitors *and* backgrounded Task agents are re-armed. Because a
   side-effectful agent cannot be safely *blind*-rerun, non-Monitor re-arm carries a verify-first
   guard (§ Re-arm turn).

## Architecture

### 1. Observation layer — `BackgroundTaskTracker`

A passive observer fed by the message pump, maintaining per-session a map of **live** background
tasks. It is the single source of truth for "what was running"; it owns no policy.

- **Launch detected** — a `tool_use` block that starts a background task (Task/Monitor with the
  background flag). Record `{tool_use_id, tool_name, kind, input_json (verbatim), launched_at}`.
  `kind ∈ {monitor, task}` is classified from the tool name/shape.
- **Termination detected** — a `<task-notification>` block carrying a terminal `<status>`
  (`killed` | `stopped` | `completed`) and a `<tool-use-id>`. The `tool-use-id` is the join key back
  to the launch (confirmed present in both the launch block and the notification, e.g.
  `toolu_014Yy41KeVc2A.JjQ8kpt6F1C`). On terminal status, drop the task from the live map.
- **Live** = launched and not yet terminally notified.
- **Defensive parsing** — if a block can't be parsed against the expected shape, capture nothing and
  log at debug. A drift in harness shape degrades to the "warn / lose nothing else" path; it never
  crashes the pump.

### 2. Persistence

Live records must survive the restart, so they go to SQLite. Per project norms (`init_db` backfills
missing **nullable** columns; `create_all` never ALTERs — see CLAUDE.md "Dev / testing"), this is
**additive and nullable**: a new `DbBackgroundTask` table

`(id, session_id, tool_use_id UNIQUE, tool_name, kind, input_json, launched_at, rearmed_at NULL)`.

The tracker upserts on launch and **deletes on terminal notification**, so at any instant the table
holds exactly the live set. `rearmed_at` is stamped when a re-arm turn for that row is dispatched (see
dedup).

### 3. Re-arm on restart (auto, all sessions with live tasks)

`rehydrate()` gains a second pass: for each session that has rows in `DbBackgroundTask`, **eagerly**
`ensure_driver()` (resume from transcript) and enqueue exactly one **re-arm turn** for that session
(batching all of that session's live tasks into the one turn).

This is a deliberate change to the lazy-rehydrate contract: sessions with live tasks now spawn a
driver and run a turn at boot, before any user attaches.

### 4. The re-arm turn

One injected turn per session, listing each live task with its verbatim captured `input_json`:

- **`kind == monitor`** — *"These background monitors were stopped by a server restart. Relaunch each
  with exactly these parameters: `<input_json>`."* Relaunch is idempotent; an already-satisfied
  monitor (e.g. the PR merged during downtime) fires once and stops.
- **`kind == task`** — same instruction, prefixed with the **interrupted-verify guard**: *"This task
  was interrupted by a server restart and may have partially completed. Before redoing work, check
  current state (e.g. `git status`, what already exists) and continue from where it left off rather
  than restarting blindly."*

### 5. Idempotency & dedup

- On dispatch of a session's re-arm turn, stamp `rearmed_at` on its rows and clear them from the live
  set, so a subsequent attach or queued-prompt resend cannot double-launch.
- Dedup is keyed by `tool_use_id`; a task terminally notified before the restart is never re-armed
  (it was already removed from the table).
- Relaunching an already-satisfied monitor is benign (single fire, then stop).

### 6. Failure handling

If a re-arm turn cannot run — account needs reauth, quota exhausted, driver spawn fails — **do not
drop the rows**. Leave them persisted (un-stamped) and fall back to the **warn path**: surface
"stopped by deploy — tap to relaunch" in the session, and retry re-arm on next attach. Loss is
always visible and recoverable.

### 7. Interaction with drain, rotation, quota

- Re-arm turns run **after** the restart completes, so they do not contend with the drain that just
  finished. They briefly mark their sessions busy; the *next* deploy's drain handles them normally.
- Each re-arm runs under the session's **bound account**, consuming that account's quota and
  participating in normal rotation/failover. Auto-re-arm across many sessions is therefore a real
  quota cost on every redeploy — we `log()` the count of sessions/tasks re-armed so it is visible,
  and bound concurrent re-arms with a small cap to avoid a thundering herd at boot.

## Testing (TDD)

- **Tracker:** launch block → live; matching terminal notification → cleared; unparseable/drifted
  block → no capture, no crash; `tool_use_id` join across launch and notification.
- **Persistence:** round-trip across a simulated rehydrate; table holds exactly the live set;
  terminal notification deletes the row.
- **Re-arm dispatch:** eager driver spawn only for sessions with rows; one turn per session batching
  all its tasks; verbatim `input_json` injected; verify-guard present for `kind=task`, absent for
  `kind=monitor`.
- **Dedup:** `rearmed_at` stamped on dispatch; second pass / attach does not re-inject.
- **Failure path:** re-arm failure retains rows and routes to warn; retry on attach succeeds.

## Open implementation questions (for the plan, not blockers)

- Exact tool-name/shape signature that distinguishes a *background* launch from a foreground tool
  call, and `monitor` vs `task` classification — to be pinned against a real transcript fixture.
- Whether to store on a `DbSession` JSON column instead of a side table (both satisfy the
  nullable-additive constraint; side table chosen here for clean per-task dedup).
- Concurrency cap value for boot-time re-arm.
