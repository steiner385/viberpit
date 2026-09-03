# Feature Specification: Session Lifecycle Continuity & Self-Healing

**Feature short name:** session-continuity
**Created:** 2026-09-02
**Status:** Draft
**Constitution:** Governed by `.specify/memory/constitution.md` v1.1.0 (Principles I–VII)
**Precursor:** Session-lifecycle continuity audit (2026-09-02) identifying gaps G1–G10

## Summary

A cross-cutting hardening of the session lifecycle so that every session's presented state is
truthful within a bounded staleness, every failure carries a durable, typed reason, every
lifecycle transition is explained to the user in place, and the system self-heals from every
crash axis — daemon restart, daemon crash, dead fleet host, database write failures, and stuck
spawns — without human intervention where safe, and with full context preserved for the human
where their judgment is required.

The audit found that the existing architecture recovers well wherever state is *derived* (the
`derive_status()` chain, the #939 undelivered-prompt predicate) and lies or loses things wherever
a stored shadow is trusted too long. This feature extends the derivation philosophy to liveness
itself: a fenced per-host heartbeat becomes the one mechanism from which the UI's honesty, the
dead-host adoption trigger, and split-brain protection are all derived.

Ten gaps are closed, grouped in three themes:

1. **Liveness never reaches the session card** — G1 (dead fleet host shows "Working…" forever),
   G2 (dead local daemon leaves pulsing-green pills), G3 (session list and live socket can
   disagree), G8 (unknown status renders as calm "Ready").
2. **Failures that defer or silence themselves** — G4 (broken-at-birth sessions land "Ready"),
   G5 (no deadline on spawn; a hung spawn is invisible to every watchdog), G6 (async
   `queued → ended` stores no failure reason).
3. **Transitions the user isn't told about** — G7 (pending permissions/questions/plans are lost
   on restart), G9 (a failed delete silently resurrects the session), G10 (completion
   auto-archive makes sessions vanish unexplained).

## Clarifications

### Session 2026-09-02

- Q: Ambition level — honest UI only, honesty plus cheap durability, or full self-healing?
  → A: **Full self-healing** — truthful presentation everywhere, durable holds and failure
  provenance, automatic retry of failed/stuck spawns, and automatic adoption of sessions off
  dead fleet hosts.
- Q: What does reassignment off a dead host mean, given the worktree and transcript live on that
  host's disk? → A: **Adopt + best-effort resume** — the primary adopts orphaned rows, attempts
  a transcript-based resume on a healthy host when the conversation state is reachable via the
  shared account backups, and otherwise restarts fresh with a continuity nudge (repo, branch,
  goal, last prompt). The user always sees an explicit "moved from <host>" event.
- Q: Liveness mechanism? → A: **Fenced host heartbeats in the DB** (architecture A) — extend the
  proven `primary_lease` fenced-write pattern to a per-host heartbeat table; per-session
  liveness is derived from the owning host's heartbeat, never stored. Rejected: edge-side-only
  derivation (no fence, primary cannot see worker liveness) and per-session heartbeats (write
  amplification; the hung-driver case is already covered by the stall watchdog).
- Q: Auto-resume default? → A: **On by default** for work statuses (`in_progress`, `working`,
  `condensing`), extended to waiting statuses via the durable-hold path; per-session veto
  retained. Opt-in auto-resume contradicts full self-healing.
- Q: Should waiting-status sessions (pending permission/question/plan) auto-resume after the
  hold content is durable? → A: **Yes** — the resume nudge includes the lost hold content and
  instructs the agent to re-raise the question if still relevant; the system never answers on
  the user's behalf.

## User Scenarios & Testing

### Primary user story

The user opens the dashboard at any moment — mid-deploy, after a power cut on a fleet host,
after a daemon crash, during a network partition — and every session card tells the truth about
whether the process behind it can act, what happened to it while the user was away, and what (if
anything) the user must do next. Sessions interrupted by infrastructure failures come back on
their own, with their conversation resumed where possible, and anything the agent was asking the
user at the moment of failure is re-presented rather than lost.

### Acceptance scenarios

1. **Dead fleet host, watching user.** A worker host loses power mid-turn. Within the staleness
   bound the session card changes from "Working…" to "Unreachable — last seen Xm ago"; after the
   dead threshold the primary fences the host and adopts the session; the card shows
   "Moved from <host>", then resumes (transcript reachable) or restarts with a continuity nudge
   (transcript not reachable); a timeline event records the move. No card ever shows a pulsing
   busy state for a process that cannot be confirmed alive within the bound.
2. **Partitioned host returns.** The "dead" host was network-partitioned, not dead, and its
   claude processes kept running. On reconnecting, its heartbeat renewal fails against the
   bumped fence; the daemon kills its now-foreign drivers without persisting their status,
   re-acquires with the new fence, and rehydrates only rows still stamped to it. No session is
   ever driven by two hosts.
3. **Daemon restart with a pending question.** The user had an "Answer needed" card when the
   daemon restarted. Afterward the session shows the exact lost question ("When the daemon
   restarted, the agent was asking: …"), and auto-resume re-runs the turn with a nudge carrying
   that content so the agent re-asks. The user's decision is preserved as theirs to make.
4. **Spawn that would have hung forever.** An auth proxy wedges during spawn. The spawn deadline
   fires, the driver is torn down, a typed `spawn_failed(spawn_timeout)` event is recorded, one
   retry runs on a rotated account, and only then does the session land `ended` — presented as
   "Failed to start: spawn timeout" with a retry affordance, never as a hung create request.
5. **Queued session fails to start.** A queued session's async promotion hits a missing branch.
   The session shows "Failed to start: branch switch failed" (error tier) instead of a silent
   gray "Ended"; transient reasons (clone/network) re-queue once with backoff before failing.
6. **Delete over a stale sshfs handle.** The user deletes an archived session; the rmtree is
   incomplete. The card shows "Deleting… (retrying)" instead of vanishing and resurrecting; if
   the delete poisons after repeated strikes, the card escalates to "Delete stuck — needs manual
   cleanup".
7. **Completion auto-archive.** A session completes and is auto-archived because the goal bar was
   met. The user sees "Auto-archived: goal met" in place with an Undo affordance; undo unarchives
   and pins the existing sticky opt-out so automation will not re-archive it.
8. **Version-skew deploy.** The backend ships a new status value before the frontend learns it.
   The card shows a neutral "Unknown" presentation — never a reassuring "Ready".
9. **DB write failure mid-session.** A status write fails. The in-memory truth continues driving
   the live view; the dirty-record flusher heals the DB shadow within one flush tick (~10 s);
   the session list and the open session view never disagree for longer than the flush interval
   plus one poll.

### Edge cases

- Heartbeat table unreachable (DB down): daemons keep running sessions; liveness at the edge
  degrades to "stale/unknown" presentation rather than false-fresh; no adoption is triggered on
  DB-derived silence alone while the primary itself cannot renew its own lease.
- Two hosts race to declare the same host dead: the conditional fence-bump write admits exactly
  one winner; adoption is additionally gated on holding the primary lease.
- Adoption while capacity is full: adopted sessions land `queued` with their interruption
  provenance intact and spawn as slots free.
- Dead host's disk held unpushed commits: the janitor's existing keep-dirty/keep-unpushed rules
  preserve the worktree for the host's return; the continuity nudge names the original branch so
  the possibility of unpushed work is surfaced, not silently forgotten.
- Hold resolved in the same instant the daemon dies: hold writes are created-then-resolved; an
  unresolved hold with a settled turn (outcome recorded) is treated as resolved and produces no
  `hold_lost` event.
- Clock skew between hosts: liveness thresholds are evaluated against DB time, not host wall
  clocks.
- Single-host deployment: no adoption target exists; sessions present honest "Unreachable" until
  systemd revives the daemon, whereupon rehydrate plus default-on auto-resume self-heal.
- Session completed/archived on a host that later dies: terminal states are never demoted to
  "Unreachable" — a finished session is truthfully finished without a live daemon.

## Requirements

### Functional Requirements

**Liveness layer (closes G1, G2; enables adoption)**

- **FR-101**: Every daemon MUST stamp a per-host heartbeat row (`host`, `beat_at`, `fence`,
  `epoch`) at a fixed interval (default 10 s, `CCSWITCH_HOST_HEARTBEAT_INTERVAL_S`) using a
  fenced conditional write (`WHERE host = ? AND fence = ?`), following the `primary_lease`
  pattern. A failed renewal MUST be treated as "I have been fenced" (see FR-202).
- **FR-102**: Per-session liveness MUST be derived, never stored, by a pure function of
  (owning host's heartbeat, now) with states `fresh` (≤ 2× interval), `stale` (past fresh, under
  dead threshold), `unreachable` (past dead threshold, default 300 s,
  `CCSWITCH_HOST_DEAD_AFTER_S`), and `dead` (fenced/declared). A sixth state `unknown` is
  reported when the heartbeat snapshot itself could not be read (DB unreachable at the edge):
  distinct from a missing row (which is `fresh` — absence of evidence is not evidence of death,
  NFR-3), because a failed snapshot is no evidence *either way* and MUST NOT be reported as
  `fresh` (NFR-5). Thresholds MUST be evaluated against DB time — both `beat_at` (written with the
  DB clock) and `now` (read from the DB clock) come from the database, so a host whose local clock
  differs from the reader is never falsely demoted.
- **FR-103**: Session API payloads MUST carry `liveness` and `lastConfirmedAliveAt`, computed at
  the edge from the heartbeat table.
- **FR-104**: The frontend MUST demote the presentation of any non-terminal session to a
  distinct "Unreachable — last seen X ago" presentation when liveness is not `fresh` — a
  "Working…" pulse and a "Ready"/"Your turn" prompt affordance are equally promises a dead
  daemon cannot keep. Terminal states (`complete`, `ended`, `archived`) MUST NOT be demoted; a
  finished session is truthfully finished without a live daemon.

**Fenced adoption & cross-host resume (closes G1's stranding; self-healing)**

- **FR-201**: Only the daemon holding the primary lease MAY perform adoption. A periodic pass
  MUST detect hosts whose heartbeat is past the dead threshold and which still own non-terminal
  sessions, and MUST fence such a host by bumping its heartbeat fence with a conditional write
  before touching any of its sessions.
- **FR-202**: A daemon whose heartbeat renewal fails MUST immediately stop persisting status for
  its sessions, terminate its live drivers, re-acquire its heartbeat row at the new fence, and
  rehydrate only sessions still stamped to it.
- **FR-203**: Adoption MUST, per orphaned non-terminal session: record a durable
  `interrupted(host_lost, from_status)` event, normalize status via the same rehydrate
  normalization used for daemon restarts, and restamp `session.host` to a healthy host chosen by
  the existing ranked-host logic.
- **FR-204**: After adoption the system MUST attempt a transcript-based resume on the new host
  (account config composed from shared backups; resume iff the `claude_session_id` transcript is
  present), falling back to a fresh spawn on a re-created worktree with a continuity nudge
  naming the original host, branch, goal, and last prompt. Either path MUST record a
  `moved_host` event.
- **FR-205**: Adoption MUST respect existing capacity limits (over-cap adoptions land `queued`
  with provenance intact) and existing crash caps.

**Durable provenance & holds (closes G4, G5, G6, G7)**

- **FR-301**: A durable, append-only `session_events` record (`session_id`, `at`, `kind`,
  `payload`) MUST capture: `spawn_failed`, `interrupted`, `hold_lost`, `moved_host`,
  `auto_resumed`, `rotated_account`, `auto_archived`, `delete_retry`. This is the single source
  of truth for lifecycle history; no parallel per-feature flags may be introduced for the same
  facts.
- **FR-302**: Every async spawn failure (queued promotion included) MUST write
  `spawn_failed` with the typed reason before the session leaves `queued`/`idle`. Transient
  reasons (clone/network class) MUST be retried once with backoff before the session is marked
  `ended`; permanent reasons (e.g., missing branch) fail immediately.
- **FR-303**: `driver.start()` MUST run under a deadline (default 120 s,
  `CCSWITCH_SPAWN_DEADLINE_S`). On timeout: tear down the partial driver, record
  `spawn_failed(spawn_timeout)`, retry once on a rotated account, then mark `ended`.
- **FR-304**: A provider preflight MUST run inside spawn before the session lands `idle`:
  Copilot via the existing proxy health gate; Anthropic via a local credential check on the
  composed config (no API call). Preflight failure MUST trigger typed failure plus automatic
  account rotation and retry — never a session presented as "Ready" that cannot take a turn.
- **FR-305**: Pending permissions, questions, and plan approvals MUST be persisted as holds
  (`session_id`, `kind`, `content`, `created_at`, `resolved_at`) at creation and marked resolved
  on answer. On rehydrate, an unresolved hold whose turn died MUST produce a `hold_lost` event
  carrying the full content snapshot.
- **FR-306**: Auto-resume MUST be enabled by default for work statuses and MUST extend to
  waiting statuses whose lost hold content is available; the resume nudge MUST include that
  content and instruct the agent to re-raise the question if still relevant. The system MUST
  NOT answer a hold on the user's behalf. A per-session veto and the existing global kill
  switch remain.
- **FR-307**: A failed status persist MUST mark the in-memory record dirty; a periodic flusher
  (default 10 s) MUST retry dirty records so DB-shadow divergence is bounded by one flush
  interval.

**Truthful frontend (closes G3, G8, G9, G10)**

- **FR-401**: All presentation changes MUST route through the existing single status map
  (`statusTier.ts`) as a composition step (base → PR triage → liveness demotion); no parallel
  status mapping may be introduced.
- **FR-402**: The unknown-status fallback MUST render a neutral "Unknown" presentation (no
  pulse, non-reassuring), never "Ready".
- **FR-403**: Surface trust order MUST be deterministic: an open, fresh WebSocket for a session
  outranks the polled DB shadow; the poll outranks a closed or stale socket.
- **FR-404**: Permanent delete MUST report incomplete removal (202/`pending`); the frontend MUST
  show "Deleting… (retrying)" instead of optimistic removal for that response, and MUST surface
  the poisoned state as "Delete stuck — needs manual cleanup".
- **FR-405**: Completion auto-archive MUST be announced in place ("Auto-archived: goal met")
  with an Undo affordance wired to the existing unarchive path (which pins the sticky re-archive
  opt-out); archived cards MUST show `archived_by` provenance.
- **FR-406**: A session with a `hold_lost` event MUST display the lost hold content prominently
  with the resume affordance.
- **FR-407**: The session detail view MUST render a compact timeline from `session_events`
  (collapsed by default).

### Key Entities

- **HostHeartbeat** — `host` (PK), `beat_at`, `fence` (monotonic), `epoch`. Written only by the
  named host (fenced renew) or by the primary (fenced declare-dead bump).
- **SessionEvent** — `id`, `session_id` (FK), `at`, `kind`, `payload` (JSON). Append-only.
- **SessionHold** — `id`, `session_id` (FK), `kind` (`permission`|`question`|`plan`),
  `content` (JSON), `created_at`, `resolved_at` (nullable).
- **Session (existing)** — gains no new stored status; `host` restamping on adoption is the only
  new write path. All new columns/tables are nullable-or-defaulted so schema reconcile applies
  them on both dialects; both hand-maintained Postgres test lists and a chained alembic revision
  are updated.

## Non-Functional Requirements

- **NFR-1 (Truth bound)**: No busy/needs-you presentation may persist for a session whose owning
  daemon has not been confirmed alive within 2× heartbeat interval + one edge poll cycle.
- **NFR-2 (Single-writer)**: At most one daemon may drive a session at any instant, guaranteed by
  fencing, under arbitrary combinations of crash, restart, and partition.
- **NFR-3 (No false takeover)**: The dead threshold MUST be tunable and default well above known
  benign blips (~10 s tailnet settling); false takeovers under a healthy-but-slow network are
  treated as defects.
- **NFR-4 (Write cost)**: The liveness layer adds O(hosts) writes per interval, never
  O(sessions).
- **NFR-5 (Availability over consistency)**: DB unavailability never halts running sessions;
  degradation is visible (stale/unknown liveness), never false-fresh.
- **NFR-6 (Constitution)**: No claude binary modification (I); per-process account isolation
  unchanged (II); behavior verified against real components in integration drills (III); TDD and
  the four-step lint-and-test gate (V); every replaced code path deleted in the same change (VI).

## Success Criteria

- Every acceptance scenario above passes as an automated integration drill.
- Zero UI states in which a pulsing busy pill can describe a process not confirmed alive within
  the truth bound (verified by the liveness-demotion test matrix).
- A daemon restart or crash loses zero pending holds: every unresolved hold at death is either
  provably resolved or re-presented with full content.
- Fleet drill: killing a worker host with live sessions results in all its non-terminal sessions
  adopted, each with a `moved_host` event, and no double-driven session when the host returns.
- All spawn paths are watchdog-covered: no code path can leave a create request or queued
  promotion hung or silently `ended` without a typed `spawn_failed` event.

## Assumptions

- The sshfs-shared `backups/` remains the cross-host source for composing account config; when
  it is unreachable, best-effort resume degrades to fresh-restart (already specified).
- The claude-agent-sdk resume-by-session-id mechanism works when pointed at a composed config
  dir containing the transcript (verified in the plan phase against the real binary per
  Principle III before the adoption workstream lands).
- DB time is a usable common clock across hosts (all daemons share one DB).

## Dependencies

- Existing: `primary_lease.py` (fence pattern), rehydrate normalization, `_recover_undelivered_prompts`,
  ranked-host selection, `compose()`, worktree janitor keep-rules, sticky unarchive opt-out,
  batch/auto resume (#938), durable initial-prompt recovery (#939).
- Schema: reconcile-driven column/table addition; `_REQUIRED_EXTRAS` and (if needed)
  `RECONCILE_EXEMPT` test lists; alembic revision chained off newest.

## Out of Scope

- Cross-host migration of *live* worktree contents (unpushed commits move only when the host
  returns).
- Per-session (driver-level) heartbeats — the stall watchdog already covers hung drivers on
  healthy hosts.
- A full durable outbox/journal for status writes — the dirty-flusher bound is sufficient.
- Passkey/public-hosting auth work (feature 001, P4).
- Any change to Claude binary invocation or account isolation semantics.
