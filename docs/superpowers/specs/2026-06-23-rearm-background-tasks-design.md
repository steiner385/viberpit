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
3. **Scope: all tasks auto** — Monitors, backgrounded `Task` agents, *and* backgrounded `Bash`
   commands are re-armed. Because a side-effectful task cannot be safely *blind*-rerun, non-Monitor
   re-arm carries a verify-first guard (§ The re-arm turn).
4. **Liveness signal: snapshot at drain** — the set to re-arm is captured when the deploy calls
   `begin_drain` (the daemon's own "about to restart" signal), *not* inferred from `killed` markers
   after the fact. This scopes re-arm precisely to deploy restarts and sidesteps the
   `killed`-ambiguity (see below).

## Ground truth (verified against real transcripts)

Confirmed by inspecting live `.jsonl` transcripts under `~/.claude*/projects` and the per-account
config dirs:

- **A background launch is an `assistant` `tool_use` block.** Two shapes:
  - **Monitor:** `name == "Monitor"`, input `{description, persistent, timeout_ms, command}`.
  - **Backgrounded Bash:** `name == "Bash"`, input `{command, description, run_in_background: true}`.
  - (A backgrounded `Task` agent is the same pattern with `name == "Task"`.)
- **A notification is a `queue-operation` (`operation: "enqueue"`) entry** whose `content` is the
  `<task-notification>…</task-notification>` XML. Terminal ones carry `<tool-use-id>` + `<status>`;
  the join key back to the launch is `<tool-use-id>`. Mid-run "Monitor event:" notifications carry
  **no** status (still running).
- **Observed terminal statuses:** `completed` (~24k), `killed` (~7k), `failed` (~2k). `killed` *is*
  durably written — but it is **ambiguous** (a user Esc / manual stop also produces `killed`), and the
  transcript **accumulates every generation** (a monitor re-armed across N deploys leaves N killed
  launches). Re-arming off `killed` would resurrect deliberately-stopped tasks and duplicate stale
  generations. Hence Decision 4: snapshot the *open* set at drain instead.

**Single source of truth.** Both launches and terminations already live durably in the transcript
`.jsonl`. We therefore do **not** add a live observer or a mirror table (that would be a parallel
implementation of transcript state — forbidden by constitution Principle VI / CLAUDE.md "find the
existing source of truth"). We reuse the existing transcript reader (`server/sessions/replay.py`
patterns) to scan, and persist only a small transient *snapshot* keyed to a deploy event.

## Architecture

### 1. Scanner — `server/sessions/background_tasks.py` (new, pure)

Pure, side-effect-free functions over a transcript path:

- `scan_open_background_tasks(jsonl_path) -> list[OpenBgTask]` — parse the `.jsonl` (same line-by-line
  tolerance as `replay._build_events`: skip blank/`isSidechain`, normalize `message.content`).
  - **Launches:** `assistant` `tool_use` where `name == "Monitor"` **or**
    `input.get("run_in_background")` is truthy. Record `{tool_use_id, kind, name, input, description}`,
    `kind = "monitor" if name == "Monitor" else "task"`.
  - **Terminations:** `queue-operation` entries whose `content` contains `<task-notification>` with a
    `<tool-use-id>` and a `<status>` in `{completed, failed}`. (`killed` is **not** treated as
    terminal here — at drain time a deploy kill has not happened yet; an *open* task is simply one not
    yet completed/failed.)
  - **Open** = a launch whose `tool_use_id` has no `completed`/`failed` termination. Returns the open
    set, newest-first, **deduped by `tool_use_id`**.
- `build_rearm_prompt(tasks: list[OpenBgTask]) -> str` — assemble the single injected turn (§4).
- **Defensive parsing** — a malformed line/block is skipped, never raised. Drift in harness shape
  degrades to "snapshot nothing for that session" (warn path), never a crash.

### 2. Persistence — transient drain snapshot (nullable, no migration)

Add one **nullable JSON** column to `Session` (`server/db/models.py`):
`pending_rearm: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)` — backfilled
by `init_db`'s reconcile (CLAUDE.md "Dev / testing"; matches the existing `sub_agents` column
pattern). It holds the open-task snapshot `[{tool_use_id, kind, name, input, description}, …]` for a
session, or `NULL` when nothing is pending. This is **deploy-orchestration state, not a mirror** of
the transcript: written only at drain, cleared on restart-rearm or on drain-cancel.

### 3. Snapshot at drain — extend `begin_drain()`

`server/sessions/orchestrator.py` `begin_drain()` gains a snapshot pass: for each session with a
**live driver** (`self._drivers` — only a running process can have live background tasks), resolve its
transcript via `transcript_path()`, `scan_open_background_tasks()` in a thread, and persist the result
to that session's `pending_rearm` column. Runs concurrently (`asyncio.gather` over live sessions),
bounded by the drain window.

`cancel_drain()` (and therefore `_drain_selfclear`) **clears all `pending_rearm` snapshots** — so an
aborted/self-cleared drain that never restarts leaves nothing armed. A snapshot is live only while the
daemon is genuinely draining toward a restart.

### 4. Re-arm on restart — `rearm_background_tasks()`

A new orchestrator method, called from the **daemon** path (`server/ipc/serve.py`) immediately after
`rehydrate()` (the daemon owns the real drivers; the edge app's `app.py` rehydrate is a no-op for this
in remote mode). For each session whose row has a non-empty `pending_rearm`:

1. `ensure_driver()` — eagerly resume from transcript (deliberate change to lazy-rehydrate: sessions
   with pending re-arm now spawn a driver at boot, before any attach).
2. `send_prompt(build_rearm_prompt(tasks))` — one batched re-arm turn.
3. Clear `pending_rearm` (set `NULL`) and add the session to an in-memory `_rearmed` set.

Bounded by a small concurrency cap (`CCSWITCH_REARM_CONCURRENCY`, default 3) to avoid a thundering
herd at boot. The count of sessions/tasks re-armed is `log()`-ged for visibility.

### 5. The re-arm turn — `build_rearm_prompt`

One injected turn per session, listing each open task with its verbatim captured `input`:

- **`kind == "monitor"`** — *"These background monitors were stopped by a server restart. Relaunch
  each with exactly these parameters: `<input>`."* Relaunch is idempotent; an already-satisfied
  monitor (e.g. the PR merged during downtime) fires once and stops.
- **`kind == "task"`** — same instruction, prefixed with the **interrupted-verify guard**: *"This task
  was interrupted by a server restart and may have partially completed. Before redoing work, check
  current state (e.g. `git status`, what already exists) and continue from where it left off rather
  than restarting blindly."*

### 6. Idempotency & dedup

- `pending_rearm` is cleared on successful dispatch; the in-memory `_rearmed` set prevents a
  concurrent attach (§7) from double-firing within the same process.
- The snapshot is deduped by `tool_use_id` and captures only the *currently-open* generation, so
  accumulated historical `killed` launches never re-arm.
- Relaunching an already-satisfied monitor is benign (single fire, then stop).

### 7. Failure handling & retry-on-attach

If a re-arm turn cannot run (account needs reauth, quota exhausted, driver spawn fails), **do not
clear `pending_rearm`** — log and leave it. It is retried two ways: (a) the next daemon restart
re-scans and re-dispatches; (b) `ensure_driver()` gains a tail hook — when a user attaches a detached
session whose row has a non-empty `pending_rearm` not in `_rearmed`, it dispatches the re-arm then.
The killed task's own `killed` notification remains visible in the transcript, so loss is never
silent.

### 8. Interaction with drain, rotation, quota

- Re-arm turns run **after** the restart completes, so they do not contend with the drain that just
  finished. They briefly mark their sessions busy; the *next* deploy's drain handles them normally.
- Each re-arm runs under the session's **bound account**, consuming that account's quota and
  participating in normal rotation/failover. Auto-re-arm across many sessions is a real quota cost on
  every redeploy — hence the concurrency cap and the visibility `log()`.

## Testing (TDD)

- **Scanner (`background_tasks.py`):** against transcript fixtures —
  - Monitor launch (`name="Monitor"`) and backgrounded Bash launch (`run_in_background:true`) are
    both detected; foreground Bash is ignored.
  - A launch with a `completed`/`failed` `<task-notification>` (joined by `<tool-use-id>`) is **not**
    open; a launch with only mid-run "Monitor event" notifications (no status) **is** open; a launch
    with a `killed` notification **is** open (killed is not terminal for the scan).
  - Deduped by `tool_use_id`; malformed lines/blocks skipped without raising.
  - `build_rearm_prompt`: verbatim `input` injected; verify-guard present for `kind="task"`, absent
    for `kind="monitor"`.
- **Drain snapshot:** `begin_drain()` writes `pending_rearm` for live-driver sessions with open
  tasks; `cancel_drain()` and `_drain_selfclear` clear all snapshots.
- **Persistence:** `pending_rearm` round-trips DB → `rehydrate()`; `init_db` reconcile backfills the
  column on a pre-existing SQLite DB (schema-drift test stays green).
- **Re-arm dispatch:** eager `ensure_driver` only for sessions with a non-empty `pending_rearm`; one
  batched turn per session; `pending_rearm` cleared and `_rearmed` set on success; concurrency cap
  respected.
- **Failure path:** dispatch failure retains `pending_rearm`; a later `ensure_driver` attach
  re-dispatches; `_rearmed` prevents a double-fire within one process.

## Open implementation questions (for the plan, not blockers)

- Whether a backgrounded `Task` agent's `tool_use` carries `run_in_background` in input the same way
  Bash does, or needs a name-based rule — pin against a fixture during Task 1; default rule
  (`name=="Monitor"` OR `input.run_in_background`) already covers Monitor + Bash, the observed cases.
- Concurrency cap default (3) — tune after observing real redeploy fan-out.
