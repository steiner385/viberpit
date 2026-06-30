# PR Doctor — Design

**Date:** 2026-06-30
**Status:** Approved (brainstorming) → ready for implementation plan
**Roadmap:** Second of three Automation-tab specs. Builds on
`2026-06-30-automation-tab-shell-design.md` (the registry/contract/spawn/per-target-state shell).
Consumes, does not extend, the shell.

## Problem

The shell migrated the existing pr_autofix into the registry automation `pr-doctor`, but kept its
behavior minimal: it only reacts to **failing CI**, only when the owning session still has a **live
driver**, with a **single** attempt and no recovery for **merge conflicts**, **out-of-date
branches**, or **stuck checks**. A PR created via ccswitch whose session was archived, or that broke
in any way other than a failed check, rots silently.

PR Doctor turns `pr-doctor` into a real CI/CD recovery loop: it classifies *why* a PR is unhealthy,
routes each failure mode to an appropriate recovery strategy (some mechanical and quota-free, some
agent-driven), recovers PRs even when the owning session is gone, and bounds its own retries so it
never loops forever.

## Goals

- Detect and recover four PR failure modes: **failing CI**, **out-of-date branch**, **merge
  conflict**, **stuck/queued checks**.
- Recover a PR even when its owning session is archived/closed — spawn a fresh dedicated fix session
  (via the shell's de-duped `spawn_session`).
- Route failure mode → strategy: **mechanical** strategies (branch update, re-run checks) are direct
  `gh` calls that spend **no** Claude session; **session** strategies (CI fix, conflict resolution)
  go through the owning live driver or a spawned session.
- Be safe: risk-tiered (`auto` gates risky modes to `propose` unless explicitly opted in), bounded
  retries with give-up, never act on terminal (merged/closed) PRs, never loop on an unchanging PR.
- Add **no new storage** — use the shell's per-target state, activity feed, config row, and spawn
  seam (constitution VI: one source of truth).

## Non-goals

- No new automation *framework* — that is the shell. PR Doctor only supplies behavior + config for
  `pr-doctor`.
- No recovery of PRs **not** created via ccswitch (no `Session.origin`/authored-branch lineage).
- No review-required/approval automation (a `BLOCKED` `mergeStateStatus` is surfaced/notified, not
  auto-resolved — humans approve PRs).
- No new persisted tables/columns (everything rides the shell's `automation`/`automation_event`/
  `automation_target_state`).
- No change to how sessions are spawned (the shell owns `spawn_session`).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Failure modes (v1) | Failing CI, out-of-date branch, merge conflict, stuck/queued checks |
| Detection plumbing | Extend GraphQL query + `PullRequestInfo` with `mergeable` + `mergeStateStatus` |
| Monitoring reach | Watch any **open** PR authored via ccswitch, incl. archived/closed sessions; extend the poller pairs-provider; unregister on merge/close |
| Worker resolution | Owning **live** session if a driver exists; else **spawn** a fresh de-duped session |
| Mechanical vs session | Branch-update + re-run-checks = direct `gh` (no session); CI fix + conflict resolution = session |
| Escalation | **Configurable** bounded retries (`max_attempts`, default 3; 0 = notify-only) → give up + notify |
| Risk gating | Risk tiers: mechanical modes auto by default; risky modes (conflict, optionally CI) fall back to `propose` in `auto` unless opted in |
| Re-arm | New `head_sha` or a different failure class resets `attempts`/`gave_up` |
| Terminate | On merged/closed → unregister poller pair + delete target-state row |

## Architecture

### 1. The PR Doctor engine — `server/automations/pr_doctor.py` (+ a pure `pr_doctor_engine.py`)

`_on_pr_change` in the orchestrator (today a thin notify/inject) is refactored to delegate to a
**PR Doctor engine**. Keep the orchestrator method as the poller callback (it already resolves the
owning session and owns `send_prompt`/drivers), but move the decision logic into testable units:

- `server/automations/pr_doctor_engine.py` (**pure**, no I/O): `classify(info) -> FailureMode | None`
  and `plan_recovery(mode, mode_setting, config, target_state) -> RecoveryAction`. A `RecoveryAction`
  is one of: `Notify`, `Propose(action)`, `Mechanical(kind)`, `SessionFix(kind)`, or `GiveUp` — a
  plain dataclass the orchestrator executes. This is the heart of the spec and is unit-tested without
  any GitHub/session I/O.
- `server/automations/pr_doctor.py` (the registered automation, from the shell): supplies `id`,
  `name`, `modes`, `config_schema` (§5), and `status(ctx)` (§7). It does **not** do I/O in `status`.

The orchestrator side (`_on_pr_change` / a new `_pr_doctor_step`) reads the `pr-doctor` row's mode +
config, loads the target's shell state via the pr-doctor `AutomationContext`, calls
`plan_recovery(...)`, executes the returned `RecoveryAction`, writes back target state, and emits the
activity event(s).

### 2. Detection — extend `PullRequestInfo` + the poller's reach

- **`server/github/pr_status.py`:** add to `PullRequestInfo` (defaulted, backward-compatible):
  `mergeable: str = "UNKNOWN"` (`MERGEABLE`/`CONFLICTING`/`UNKNOWN`) and
  `merge_state_status: str = ""` (`BEHIND`/`BLOCKED`/`CLEAN`/`DIRTY`/`UNSTABLE`/`UNKNOWN`). Extend
  `build_pr_query` to request `mergeable` and `mergeStateStatus`, and `parse_pr_response` to populate
  them. `to_dict`/`from_dict` round-trip the new fields.
- **Monitoring reach (the shell's reserved seam):** today the poller pairs-provider yields only live
  sessions' `(repo, branch)`. PR Doctor contributes an additional provider that yields the
  `(repo, headRefName)` of every **open** PR authored via ccswitch (joined from sessions'
  `authored_branches` regardless of session liveness), so an archived session's open PR keeps being
  polled. On a PR going `merged`/`closed`, its pair drops out (provider stops yielding it), so the
  poll set self-prunes.

### 3. Failure-mode → strategy router (pure)

`classify(info)` returns the first matching failure mode in priority order (so we never fight CI on a
conflicted branch):

| Priority | Failure mode | Detection | Strategy | Spends a session? |
|---|---|---|---|---|
| 1 | `merge_conflict` | `mergeable == "CONFLICTING"` | spawn/inject "rebase onto base & resolve" | **yes** (risky) |
| 2 | `out_of_date` | `merge_state_status == "BEHIND"` | `gh` update-branch (merge base → head) | **no** (mechanical) |
| 3 | `stuck_checks` | a required check `QUEUED`/`overdue` past ETA, none failing | `gh` re-run the stuck workflow run | **no** (mechanical) |
| 4 | `failing_ci` | `checks == "failing"` | spawn/inject the existing branch-aware fix prompt | **yes** |

`merged`/`closed`/`draft`-only or fully `passing` + `CLEAN` → no failure mode (terminate / idle).
`BLOCKED` (review required) → `Notify` only (non-goal to auto-approve).

Mechanical strategies are direct `gh` calls (`gitrepos._run_gh`, reusing the existing gh wrapper):
- `out_of_date` → `gh api -X PUT repos/{repo}/pulls/{n}/update-branch` (or `gh pr update-branch`).
- `stuck_checks` → `gh run rerun <run-id> --failed` / re-dispatch; identify the stuck run from
  `checks_detail`.

### 4. Worker resolution (session strategies only)

A generalized `request_pr_fix(repo, pr, *, kind)` (the existing method, extended):
1. If the owning session (matched by `(repo, branch)`) has a **live driver** → inject via
   `send_prompt` (today's path; preserves session context; queues behind an in-progress turn).
2. Else → `ctx.spawn_session(origin_ref=f"{repo}#{pr.number}", dedupe_key=f"{repo}#{pr.number}", …)`
   — a fresh session whose first prompt is the branch-aware prompt (`gh pr checkout` first). De-dup
   guarantees a single in-flight fix session per PR despite the 75s poll cadence.

The conflict-resolution prompt is a new branch-aware variant of `build_pr_fix_prompt` ("rebase onto
the base branch, resolve conflicts, push"); the CI-fix prompt is the existing one unchanged.

### 5. Modes + risk gating + config schema

Master `mode ∈ {off, propose, auto}` (inherited). Config rendered by the shell's schema panel:

| key | type | default | meaning |
|---|---|---|---|
| `enabled_repos` | repo-multiselect | `[]` (all) | limit to these repos |
| `max_attempts` | int | `3` | give-up threshold per target; `0` = notify-only |
| `auto_update_out_of_date` | bool | `true` | mechanical: auto-update behind branches |
| `auto_rerun_stuck_checks` | bool | `true` | mechanical: auto re-run stuck checks |
| `auto_fix_failing_ci` | bool | `true` | session: auto-fix failing CI |
| `auto_resolve_conflicts` | bool | `false` | session: auto-resolve merge conflicts (risky, opt-in) |

Gating in `plan_recovery`:
- `mode == "off"` → `Notify`.
- `mode == "propose"` → `Propose(action)` for any failure mode (one-tap; never autonomous).
- `mode == "auto"` → autonomous **iff** the failure mode's flag is true **and** the repo is in scope;
  otherwise fall back to `Propose(action)`. (So `auto` + `auto_resolve_conflicts=false` surfaces a
  conflict as a propose action rather than acting.)

### 6. Retry / escalation / termination (shell per-target state)

Per-target state JSON `{attempts:int, last_failure:str, last_head_sha:str, gave_up:bool}` keyed by
`target = f"{repo}#{pr.number}"`:
- On a failure event with a chosen `SessionFix`/`Mechanical` action: if `gave_up` → skip (notify-only);
  if `attempts >= max_attempts` → emit `gave_up`, notify (needs-human), set `gave_up=true`; else act,
  and on a **real** injection/`gh` success `attempts += 1` (mechanical attempts count too) using the
  shell's mark-before-await re-arm (a draining/no-driver/`gh`-failure does not consume the attempt).
- **Re-arm:** if `info.head_sha != last_head_sha` (someone pushed) **or** the new failure class
  differs from `last_failure` → reset `attempts = 0`, `gave_up = false`. Record `last_head_sha` /
  `last_failure` each step.
- **Recovery:** transition to fully healthy (`checks == passing` and `merge_state_status in {CLEAN,
  UNSTABLE}`) after a prior attempt → emit `recovered`, reset state.
- **Terminate:** `merged`/`closed` → emit nothing new, delete the target-state row, drop the poller
  pair (§2).

The existing pr_autofix loop-guard invariants from the shell migration remain intact (one attempt per
failure event, no re-arm on transient `pending`, draining re-arms) — generalized here to N attempts.

### 7. Status & activity

- `status(ctx)` (cheap; derived from per-target state the orchestrator maintains, never a live gh
  call): text like `"watching 2 failing, 1 gave up"`, `counts = {watching, failing, gave_up,
  in_flight}`, level `active` (work in flight) / `warn` (something gave up) / `idle`.
- Events via `ctx.emit(kind=…)`: `detected`, `attempted`, `updated_branch`, `reran_checks`,
  `spawned`, `skipped_duplicate`, `recovered`, `gave_up`. Each `target`-tagged; session-linked
  (`session_id`) for `spawned`/`attempted`. The card's activity feed + `target=` filter reconstruct a
  single PR's full recovery history.

## Data flow

```
poller (75s) → PrStatusPoller(checks/state/mergeable/mergeStateStatus) for pairs from
   {live sessions} ∪ {open PRs authored via ccswitch}     ← PR Doctor extends the provider
  └─ _on_pr_change(repo, branch, old, new) → _pr_doctor_step(info):
       mode, config = read pr-doctor row
       st           = await ctx.get_target_state(target)
       reset st if head_sha changed or failure class changed     (re-arm)
       fm           = classify(info)                              (pure)
       action       = plan_recovery(fm, mode, config, st)        (pure)
       execute action:
         Notify              → push notification (debounced)
         Propose(a)          → surface one-tap action on the PR's session/pill
         Mechanical(kind)    → gh update-branch / gh run rerun; emit; attempts++
         SessionFix(kind)    → owning live driver ? inject : ctx.spawn_session(); emit; attempts++
         GiveUp              → emit gave_up; notify needs-human; st.gave_up=true
       await ctx.set_target_state(target, st)    (or delete on merged/closed)
```

## Error handling

- **`gh` mechanical call fails** (update-branch/rerun) → emit `level:"warn"` event, do **not** consume
  an attempt (re-arm), fall through to notify. A repeated mechanical failure escalates via the normal
  `max_attempts` path.
- **No live driver & spawn fails** → emit `error`, notify; attempt not consumed.
- **Terminal PR mid-flight** (merged/closed between poll and action) → action gated on actionable
  state (reuse the existing open/draft gate); becomes a no-op + terminate.
- **`mergeable == "UNKNOWN"`** (GitHub still computing) → treat as "no actionable conflict yet"; the
  next poll re-evaluates (never spawn a conflict-resolution session on UNKNOWN).
- **`classify`/`plan_recovery`** are pure and total (return `Notify`/`None` on anything unrecognized)
  — they never raise into the poller.

## Acceptance criteria

1. `classify` returns the correct failure mode for each of the 4 modes + `None` for healthy/terminal,
   with conflict prioritized over CI.
2. `plan_recovery` honors mode gating: `off`→Notify; `propose`→Propose for every mode; `auto`→
   autonomous only when the mode's flag is true and repo in scope, else Propose.
3. Mechanical recoveries make the `gh` call and spend **no** session; session recoveries route to live
   driver if present, else `spawn_session` (de-duped to one in-flight session per PR).
4. Bounded retries: stops at `max_attempts`, emits `gave_up` + notifies; `max_attempts=0` is
   notify-only; re-arms on new `head_sha` or changed failure class.
5. Monitoring covers an open PR whose owning session is archived; the pair is dropped on merge/close
   and the target-state row deleted.
6. `mergeable`/`merge_state_status` round-trip through `PullRequestInfo.to_dict`/`from_dict`.
7. Server `pytest` green; existing pr_autofix/pr-change tests still green (generalized, not broken).

## Testing (TDD)

**Pure engine (`server/tests/test_pr_doctor_engine.py`):**
- `classify`: a table of `PullRequestInfo` fixtures → expected `FailureMode` (incl. priority:
  conflicting+failing → `merge_conflict`; behind+passing → `out_of_date`; queued/overdue →
  `stuck_checks`; blocked → `None`+notify; merged/passing → `None`).
- `plan_recovery`: every (mode × flag × in-scope × target_state) combo → expected `RecoveryAction`,
  including give-up at threshold and the conflict→propose fallback when `auto_resolve_conflicts=false`.

**Detection (`server/tests/test_pr_status_merge_fields.py`):**
- `build_pr_query` requests `mergeable`+`mergeStateStatus`; `parse_pr_response` populates them;
  round-trip via `to_dict`/`from_dict`.

**Orchestrator integration (`server/tests/test_pr_doctor_step.py`):**
- Mechanical: `out_of_date` in `auto` → one `gh update-branch` call, no `spawn_session`, `attempts++`,
  `updated_branch` event.
- Session: `failing_ci`, no live driver → `spawn_session` once (second poll de-dups), `attempted`
  event with `session_id`; live driver present → inject, no spawn.
- Escalation: N failures → `gave_up` at `max_attempts`; `head_sha` change re-arms.
- Archived-session PR is polled + recovered; merge → pair dropped + target row deleted.
- Regression: existing pr_autofix behavior (failing CI, propose/auto/off) unchanged for the live-driver
  path.

## Out of scope / YAGNI

- Review-required/approval automation (BLOCKED → notify only).
- Recovering non-ccswitch PRs.
- Auto-merge orchestration (CLAUDE.md's `gh pr merge --auto` already covers that).
- Conflict-resolution strategy beyond "rebase onto base & resolve" (no cherry-pick/merge-strategy
  selection UI).
- New persisted tables/columns (rides the shell entirely).

## Open implementation notes

- Keep `classify` + `plan_recovery` pure and total; the orchestrator is the only I/O boundary.
- Reuse: the CI-fix prompt is `build_pr_fix_prompt` unchanged; add a sibling
  `build_conflict_fix_prompt`. The mechanical `gh` calls reuse `gitrepos._run_gh`. The notify path
  reuses `push_mod.notify` + the existing `_pushed_pr` debounce.
- The propose surface reuses the shell/pr_autofix "Fix PR #N" one-tap action, extended to carry the
  failure-mode kind so the button label matches ("Update branch", "Resolve conflicts", "Fix CI").
- Generalize the existing `request_pr_fix` rather than adding a parallel injector (constitution VI).
