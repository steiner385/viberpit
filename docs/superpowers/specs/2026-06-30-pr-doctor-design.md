# PR Doctor — Design

**Date:** 2026-06-30
**Status:** Revised per CI/CD + PM review (round 2) → ready for implementation plan
**Roadmap:** Second of three Automation-tab specs. Builds on
`2026-06-30-automation-tab-shell-design.md` (the registry/contract/spawn/per-target-state shell) and
on the shared **PR-status field extension** plan (below). Consumes, does not extend, the shell.

## Problem

The shell migrated the existing pr_autofix into the registry automation `pr-doctor`, but kept its
behavior minimal: it only reacts to **failing CI**, only when the owning session still has a **live
driver**, with a **single** attempt and no recovery for **merge conflicts** or **out-of-date
branches**. A PR created via ccswitch whose session was archived, or that broke in any way other than
a failed check, rots silently.

PR Doctor turns `pr-doctor` into a real CI/CD recovery loop: it classifies *why* a PR is unhealthy,
routes each failure mode to an appropriate recovery strategy (some mechanical and quota-free, some
agent-driven), recovers PRs even when the owning session is gone, and bounds its own retries so it
never loops forever.

## Goals

- Detect and recover three PR failure modes in v1: **failing CI**, **out-of-date branch**, **merge
  conflict**. (Stuck/queued-check recovery is deferred to v2 — see Non-goals.)
- Recover a PR even when its owning session is archived/closed — spawn a fresh dedicated fix session
  (via the shell's de-duped `spawn_session`).
- Route failure mode → strategy: a **mechanical** strategy (branch update) is a direct `gh` call that
  spends **no** Claude session; **session** strategies (CI fix, conflict resolution) go through the
  owning live driver or a spawned session.
- Be safe: risk-tiered (`auto` gates risky modes to `propose` unless explicitly opted in), bounded
  retries with give-up, never act on terminal (merged/closed) PRs, **never loop on a PR PR Doctor
  itself keeps changing** (the self-push re-arm guard, §6), and reliably reach `gave_up` so the
  Backlog Runner can observe it.
- Add **no new storage** — use the shell's per-target state, activity feed, config row, and spawn
  seam (constitution VI: one source of truth).

## Non-goals

- No new automation *framework* — that is the shell. PR Doctor only supplies behavior + config for
  `pr-doctor`.
- **Stuck/queued-check recovery is v2** (review: lowest value; needs a workflow-run `databaseId` not
  in the current GraphQL selection, and an "overdue" temporal input that would break the pure
  classifier). A re-run is cheap to do by hand; ship the three high-value modes first.
- No recovery of PRs **not** created via ccswitch (no `Session.origin`/authored-branch lineage).
- No review-required/approval automation (a `BLOCKED` `mergeStateStatus` is surfaced/notified, not
  auto-resolved — humans approve PRs).
- No new persisted tables/columns (everything rides the shell's `automation`/`automation_event`/
  `automation_target_state`).
- No change to how sessions are spawned (the shell owns `spawn_session`).

## Decisions (from brainstorming + spec review)

| Question | Decision |
|---|---|
| Failure modes (v1) | Failing CI, out-of-date branch, merge conflict (**stuck/queued checks → v2**) |
| Detection plumbing | The shared **PR-status field extension** adds `mergeable`, `merge_state_status`, `auto_merge_enabled` to `PullRequestInfo` (lands before this plan); PR Doctor only *reads* them |
| Monitoring reach | Watch any **open** PR authored via ccswitch, incl. archived/closed sessions; extend the poller pairs-provider; **exclude terminal-marked branches** so the poll set self-prunes |
| Worker resolution | Owning **live** session if a driver exists; else **spawn** a fresh de-duped session |
| Mechanical vs session | Branch-update = direct `gh` (no session); CI fix + conflict resolution = session |
| Escalation | Configurable bounded retries (`max_attempts`, default 3; 0 = notify-only) → give up + notify |
| Mechanical bound | **Separate** `mechanical_attempts` cap (default 2) so update-branch can't ride the session budget |
| Risk gating | Risk tiers: mechanical modes auto by default; conflict (and optionally CI) fall back to `propose` in `auto` unless opted in |
| Re-arm | New `head_sha` resets attempts **only if the push was external** (`head_sha ∉ {last_head_sha, last_action_sha}`); a different failure class also resets |
| Terminate | On merged/closed → emit terminal event, unregister poller pair, delete target-state row |

## Architecture

### 0. Prerequisite — the shared PR-status field extension (separate, first-landing plan)

PR Doctor and the Backlog Runner both extend `PullRequestInfo`. To avoid two plans editing
`server/github/pr_status.py` and racing on a rebase (constitution VI), a **single tiny precursor
plan** adds all three fields **before** either consumer:
- `mergeable: str = "UNKNOWN"` (`MERGEABLE`/`CONFLICTING`/`UNKNOWN`).
- `merge_state_status: str = ""` (`BEHIND`/`BLOCKED`/`CLEAN`/`DIRTY`/`DRAFT`/`UNSTABLE`/`UNKNOWN`).
  **Note:** `mergeStateStatus` is *already requested* in `_PR_NODE_FIELDS` and read for
  `derive_open_stage`, but never stored on the dataclass — the change is parse/store/round-trip only.
- `auto_merge_enabled: bool = False` (GraphQL `autoMergeRequest != null`) — consumed by the Runner.

The precursor plan covers `build_pr_query` (add `autoMergeRequest`), `parse_pr_response`, and
`to_dict`/`from_dict`, **plus a cross-RPC round-trip test** (the daemon→edge path, not just an
in-process dataclass round-trip — a dropped `from_dict` field is a silent detection blind spot).
PR Doctor then only *reads* these fields.

### 1. The PR Doctor engine — `server/automations/pr_doctor.py` + pure `pr_doctor_engine.py`

`_on_pr_change` (today a thin notify/inject) delegates to a **PR Doctor engine**. The orchestrator
method stays the poller callback (it resolves the owning session and owns `send_prompt`/drivers), but
the decisions move into pure, I/O-free units:

- `pr_doctor_engine.py` (**pure, total**): `classify(info, *, now=None) -> FailureMode | None` and
  `plan_recovery(mode, mode_setting, config, target_state) -> RecoveryAction`. `RecoveryAction` ∈
  `{Notify, Propose(kind), Mechanical(kind), SessionFix(kind), GiveUp}` — a dataclass the orchestrator
  executes. Neither function does I/O or raises into the poller (unrecognized → `Notify`/`None`).
- `pr_doctor.py` (the registered automation): `id`/`name`/`modes`/`config_schema` (§5), `status(ctx)`
  (§7). `status` does no I/O.

The orchestrator side (`_pr_doctor_step`) reads the `pr-doctor` row's mode+config, loads target state
via the pr-doctor `AutomationContext`, applies re-arm (§6), calls `classify`+`plan_recovery`, executes
the `RecoveryAction`, writes back target state, and emits the activity event(s).

**Split for delivery (two plans):** *Plan A* = the pure engine (`classify` + `plan_recovery` +
retry/re-arm/terminate state machine), unit-tested with zero I/O; *Plan B* = the orchestrator
execution layer (poller-reach extension, mechanical `gh` call, session-fix/spawn routing, status,
notify, propose surface). Plan B's **Task 1 is a fake-`gh`/fake-spawn harness** so the whole layer is
tested with no network and no real sessions.

### 2. Detection & monitoring reach

- **Fields:** read `mergeable`/`merge_state_status` from the precursor extension (§0).
- **Monitoring reach (the shell's reserved seam):** PR Doctor contributes a pairs-provider that yields
  the `(repo, headRefName)` of every **open** PR authored via ccswitch (joined from sessions'
  `authored_branches`, regardless of session liveness). **Termination is explicit, not result-driven:**
  a branch whose target-state row is marked terminal (PR merged/closed, §6) is **excluded** from what
  the provider yields — so the batched GraphQL query never keeps asking about dead PRs. A **node-cost
  guard** chunks the query if the pair count would exceed the GraphQL node-cost ceiling, so a large
  branch set can't fail the whole poll (and blind all detection).

### 3. Failure-mode → strategy router (pure, total)

`classify(info)` returns the first match in this **fixed priority order** (a single ordered constant,
not re-encoded per call site) — so we never fight CI on a branch that won't merge, and never spawn a
session for a check that doesn't block merge:

| Priority | Outcome | Detection | Strategy | Session? |
|---|---|---|---|---|
| 1 | terminal → `None` | `state ∈ {merged, closed}` | terminate (§6) | — |
| 2 | draft → `None` | `merge_state_status == "DRAFT"` or `is_draft` | idle | — |
| 3 | `Notify` (review) | `merge_state_status == "BLOCKED"` | notify only (non-goal to approve) | — |
| 4 | `merge_conflict` | `mergeable == "CONFLICTING"` **or** `merge_state_status == "DIRTY"` | spawn/inject "rebase & resolve" | **yes** (risky) |
| 5 | `out_of_date` | `merge_state_status == "BEHIND"` | `gh` update-branch (merge base→head) | **no** (mechanical) |
| 6 | `failing_ci` | a **required** check failing (`checks_detail` with `required and conclusion=="failure"`) | spawn/inject branch-aware fix | **yes** |
| 7 | healthy → `None` | `checks == passing` and `merge_state_status ∈ {CLEAN, UNSTABLE}` | recovery/idle (§6) | — |

Key corrections from review: **conflict also matches `DIRTY`** (`mergeable` lags async as `UNKNOWN`);
`UNKNOWN` mergeable with no `DIRTY` is treated as "no actionable conflict yet" (re-evaluate next poll,
never spawn a conflict session on `UNKNOWN`); **`failing_ci` fires only on a *required* failing check**
(a red non-required check = `UNSTABLE` = mergeable, no action). Stuck/queued is **not** classified in
v1.

The mechanical strategy is a direct `gh` call (reusing `gitrepos._run_gh`):
`out_of_date` → `gh api -X PUT repos/{repo}/pulls/{n}/update-branch` (or `gh pr update-branch`).

### 4. Worker resolution (session strategies only)

A generalized `request_pr_fix(repo, pr, *, kind)`:
1. Owning session (matched by `(repo, branch)`) has a **live driver** → inject via `send_prompt`
   (today's path; preserves context; queues behind an in-progress turn).
2. Else → `ctx.spawn_session(origin_ref=f"{repo}#{pr.number}", dedupe_key=f"{repo}#{pr.number}", …)`
   — a fresh session whose first prompt is branch-aware (`gh pr checkout` first). De-dup → one
   in-flight fix session per PR despite the 75s poll cadence.

Prompts: CI fix uses `build_pr_fix_prompt` unchanged; a sibling `build_conflict_fix_prompt`
("rebase onto base, resolve conflicts, push") covers conflicts.

**Propose surface for session-less PRs:** for an archived-session PR there is no dashboard pill to
hang a "Fix PR #N" action on. The propose action therefore renders on **the PR Doctor card** in the
Automation tab (a list of actionable PRs with one-tap buttons labeled by kind — "Update branch",
"Resolve conflicts", "Fix CI"), in addition to the existing live-session pill where one exists.

### 5. Modes + risk gating + config schema

Master `mode ∈ {off, propose, auto}` (inherited). Config (shell schema panel):

| key | type | default | meaning |
|---|---|---|---|
| `enabled_repos` | repo-multiselect | `[]` (all) | limit to these repos |
| `max_attempts` | int | `3` | session give-up threshold per target; `0` = notify-only |
| `mechanical_attempts` | int | `2` | separate cap for mechanical (update-branch) retries |
| `auto_update_out_of_date` | bool | `true` | mechanical: auto-update behind branches |
| `auto_fix_failing_ci` | bool | `true` | session: auto-fix failing required CI |
| `auto_resolve_conflicts` | bool | `false` | session: auto-resolve merge conflicts (risky, opt-in) |

Gating in `plan_recovery`: `off`→`Notify`; `propose`→`Propose(kind)` for any mode; `auto`→autonomous
**iff** the mode's flag is true **and** the repo is in scope, else `Propose(kind)`.

### 6. Retry / escalation / re-arm / termination (shell per-target state)

Per-target state JSON keyed by `target = f"{repo}#{pr.number}"`:
`{attempts, mechanical_attempts, total_attempts, total_mechanical, last_failure, last_head_sha,
last_action_sha, gave_up}`.

- **Attempt accounting:** a `SessionFix` that really injects, or a `Mechanical` `gh` success,
  increments **both** its re-armable counter (session→`attempts`, mechanical→`mechanical_attempts`)
  **and** a monotonic total (`total_attempts` / `total_mechanical`), using the shell's
  mark-before-await semantics (a draining/no-driver/`gh`-failure does **not** consume the attempt).
  **Async-inject reality:** injection is async — `record_attempt` records the *pre*-fix head, so when
  the agent's fix pushes a new (still-red) head the next poll can't tell "our own fix's push" from "an
  external human push" by sha, and the self-push re-arm below resets `attempts` to 0. Give-up
  therefore keys on the **monotonic, re-arm-immune total** (`total_attempts ≥ max_attempts` /
  `total_mechanical ≥ mechanical_attempts`), not the re-armable counter. It is **sticky**: once the
  ceiling is hit → emit `gave_up`, notify (needs-human), set `gave_up=true`, stop — a human who keeps
  pushing red after N attempts is already in the loop, so PR Doctor hands off rather than looping.
- **Self-push re-arm guard:** when PR Doctor pushes a fix or runs update-branch, record the resulting
  head as `last_action_sha`. Re-arm (reset the re-armable `attempts`/`mechanical_attempts` + `gave_up`,
  but **never** the monotonic `total_*` ceiling) **only** when
  `info.head_sha ∉ {last_head_sha, last_action_sha}` — i.e. a push that looks external. The re-armable
  counter drives the orchestrator's per-head one-shot guard (at most one inject per head); the
  monotonic total drives give-up (above). A *different* failure class also re-arms.
- **Recovery:** transition to healthy (priority-7) after a prior attempt → emit `recovered`, reset.
- **Terminate:** `merged`/`closed` → emit `merged`/`closed` terminal event, mark the target-state row
  terminal (so §2's provider drops it) then delete it.

### 7. Status & activity

- `status(ctx)` (cheap; from per-target state, never a live `gh` call): e.g.
  `"watching 2 failing, 1 gave up"`, `counts = {watching, failing, gave_up, in_flight}`, level
  `active`/`warn`/`idle`. The orchestrator bumps the `pr-doctor` row's `last_active_at` on **every
  poll tick that ran** (not per action), so "stalled" means the loop stopped, not "no work to do."
- Events (`ctx.emit(kind=…)`, `target`-tagged, session-linked where relevant): `detected`,
  `attempted`, `updated_branch`, `spawned`, `skipped_duplicate`, `recovered`, `gave_up`,
  `merged`/`closed`. `gave_up` carries the owning session's `origin_ref` in `detail` so the Backlog
  Runner can correlate (pipeline join key).

## Data flow

```
poller (75s) → PrStatusPoller(checks/state/mergeable/merge_state_status/auto_merge_enabled) for pairs
   from {live sessions} ∪ {open PRs authored via ccswitch, minus terminal-marked}   ← PR Doctor provider
  └─ _on_pr_change → _pr_doctor_step(info):
       mode, config = read pr-doctor row
       st           = ctx.get_target_state(target)
       re-arm st iff head_sha is an EXTERNAL push, or failure class changed
       fm           = classify(info)                              (pure)
       action       = plan_recovery(fm, mode, config, st)        (pure)
       execute:
         Notify            → push (debounced)
         Propose(kind)     → one-tap action on PR Doctor card (+ live-session pill if any)
         Mechanical(kind)  → gh update-branch; record last_action_sha; mechanical_attempts++
         SessionFix(kind)  → live driver ? inject : ctx.spawn_session(); record last_action_sha; attempts++
         GiveUp            → emit gave_up (detail.origin_ref); notify needs-human; st.gave_up=true
       ctx.set_target_state(target, st)   (or terminate+delete on merged/closed)
```

## Error handling

- **`gh` update-branch fails** → `warn` event, attempt **not** consumed (re-arm), notify; repeated
  failure escalates via `mechanical_attempts`.
- **No live driver & spawn fails** → `error` event, notify; attempt not consumed.
- **Terminal PR mid-flight** → action gated on actionable (open/draft) state → no-op + terminate.
- **`mergeable == "UNKNOWN"` and not `DIRTY`** → no actionable conflict yet; next poll re-evaluates.
- **`classify`/`plan_recovery`** are pure & total — never raise into the poller.
- **Poll node-cost exceeded** → chunk the query (§2); never let one oversized query blind all detection.

## Acceptance criteria

1. `classify` returns the right outcome for a fixture **per `merge_state_status` value** (DRAFT,
   BLOCKED, DIRTY, BEHIND, CLEAN, UNSTABLE, UNKNOWN) and required-vs-non-required failing checks, with
   conflict prioritized over CI and `UNKNOWN`-without-`DIRTY` → no conflict action.
2. `plan_recovery` honors gating for every (mode × flag × in-scope × state) combo (literal table
   fixture), incl. conflict→propose fallback when `auto_resolve_conflicts=false`.
3. Mechanical recovery makes the `gh` call, spends **no** session, bounded by `mechanical_attempts`;
   session recovery routes to live driver else `spawn_session` (de-duped to one in-flight per PR).
4. **Self-push re-arm guard:** a fix that pushes and stays red consumes an attempt and reaches
   `gave_up`; only an *external* push re-arms. `gave_up` carries `detail.origin_ref`.
5. Monitoring covers an archived-session open PR; on merge/close the branch is excluded from the
   provider and the target-state row deleted (no unbounded poll growth).
6. `last_active_at` bumps every tick that ran (stalled-detection is meaningful).
7. Server `pytest` green with **no network and no real sessions**; existing pr_autofix/pr-change
   tests still green; the full Plan B suite runs against the fake-`gh`/fake-spawn harness.

## Testing (TDD)

**Pure engine (`server/tests/test_pr_doctor_engine.py`):** the `classify` table (one row per
`merge_state_status` + required/non-required checks + priority cases) and the `plan_recovery` gating
table; the re-arm guard (external vs self `head_sha`); give-up at `max_attempts`/`mechanical_attempts`.

**Orchestrator integration (`server/tests/test_pr_doctor_step.py`, fakes for gh/spawn):**
- Mechanical: `out_of_date` in `auto` → one `gh update-branch`, no `spawn_session`,
  `mechanical_attempts++`, `updated_branch` event; bounded at the mechanical cap.
- Session: `failing_ci` (required), no live driver → `spawn_session` once (second poll de-dups),
  `attempted` event with `session_id`; live driver → inject, no spawn.
- Self-push: inject → still red → attempt consumed → `gave_up` (with `origin_ref`); external push
  re-arms.
- Archived-session PR polled + recovered; merge → branch excluded + target row deleted.
- Regression: existing pr_autofix live-driver behavior (failing CI, off/propose/auto) unchanged.

## Out of scope / YAGNI

- **Stuck/queued-check recovery (v2)** — needs run `databaseId` + an external "overdue" input.
- Review-required/approval automation (BLOCKED → notify only).
- Recovering non-ccswitch PRs.
- Auto-merge orchestration (CLAUDE.md's `gh pr merge --auto` covers it).
- Conflict-resolution strategy beyond "rebase onto base & resolve".
- New persisted tables/columns (rides the shell; reads the shared field extension).

## Open implementation notes

- Build order: **shared PR-status field extension** → **Plan A** (pure engine/state-machine) →
  **Plan B** (orchestrator execution; Task 1 = fake-gh/fake-spawn harness). Plan A and the Runner's
  Plan A can proceed in parallel once the field extension lands.
- Keep `classify`+`plan_recovery` pure/total; pin the priority order as one ordered constant.
- Reuse: CI-fix prompt `build_pr_fix_prompt` unchanged; add `build_conflict_fix_prompt`; mechanical
  `gh` via `gitrepos._run_gh`; notify via `push_mod.notify` + the existing `_pushed_pr` debounce;
  generalize `request_pr_fix` rather than adding a parallel injector (verify its signature post-shell
  migration before building on it).
- The shared field extension is owned by its own plan — Plan B must **not** edit
  `pr_status.py`'s query/parse/struct.
