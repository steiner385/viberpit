# Backlog Runner — durable claim-label lifecycle (stop skipping labeled-out issues)

**Date:** 2026-07-10
**Status:** approved for implementation (autonomous session; assumptions flagged in PR)

## Problem

The Backlog Runner reports "skipping" backlog issues — e.g. cairnea/KinDash. Live
evidence (2026-07-10): KinDash has 25 open issues; 22 wear the runner's own
`in-progress` claim label and 3 wear `needs-human`, so `query_candidates`
returns **0**. The DB holds only **one** live target-state row — the other ~21
labels are orphans: locked on GitHub, invisible to the runner.

### Root causes

1. **Handoff leak (systematic).** When a runner PR gets auto-merge armed and PR
   Doctor covers the repo, `_sweep_one` deletes the target-state row while the
   PR is still open. The merged close-out (`close_batch_issues`) only runs while
   a row exists, so any batch whose PR merges after handoff without `Fixes #N`
   leaves its issues open + `in-progress` forever. Evidence: KinDash #11499 has
   merged PR #11501 and is still open, labeled `in-progress`.
2. **Reconciliation is DB-blind to GitHub truth.** `_reconcile_dangling`
   derives orphans from DB state rows only; a label with no row (the leak
   above, or any crash/DB loss) is never healed. Orphans accumulate
   monotonically until the whole backlog is label-locked.

## Approaches considered

- **A — fix handoff only** (keep row until merge): stops the leak; never heals
  the 22 existing orphans or crash-shaped ones.
- **B — orphan sweeper only**: heals everything eventually; leaves the
  systematic leak so every batch cycles through orphan limbo.
- **C — both (chosen)**: fix the leak at the source + a GitHub-truth safety
  net. A distributed lock whose release can be interrupted needs both.

## Design (approach C)

### 1. Track batches through handoff

In `BacklogRunnerLoop._sweep_one`, when `should_free_slot(info, active)` is
true, do NOT delete the target state. Instead set `handed_off: True` on the row
and emit the `handoff` event exactly once (skip re-emit when already flagged).
The sweep keeps watching the row:

- PR **merged** → existing close-out path (close issues + strip labels), delete row.
- PR **closed unmerged** or PR Doctor **gave up** → existing needs-human path, delete row.
- Attempt-charging branch (`info is None and session ended`) must NOT fire for
  a `handed_off` row — a transient PR-lookup miss after handoff must not burn
  attempts or escalate.

Slot accounting: `_backlog_count_in_flight` and `_backlog_repo_load`
(orchestrator) exclude `handed_off` rows, so the concurrency slot frees at the
same moment it does today. `_reconcile_dangling` already treats rows with a
`session_id` as live, so handed-off rows are safe from label release.

### 2. Orphan heal sweep (GitHub truth)

New helpers in `server/automations/backlog_github.py`:

- `query_claimed_refs(repos, *, gh_run) -> list[str]` — GraphQL search for open
  issues with `label:in-progress` across the enabled repos (`first: 100`).
- `heal_orphan_claims(refs, tracked, *, gh_run, limit) -> list[tuple[str, str]]`
  — for each ref not in `tracked` (the union of all state rows' `issues`),
  resolve `find_referencing_pr(ref)`:
  - **merged** → close the issue with a linking comment + remove `in-progress`
    → action `"closed"`.
  - **open** PR → leave untouched (live work; a later pass heals post-merge).
  - **none / closed-unmerged** → remove `in-progress` (back to the candidate
    pool) → action `"reclaimed"`.
  Caps cross-ref lookups at `limit` per pass (rate-limit hygiene); returns the
  actions taken so the caller can emit events.

Loop integration (`BacklogRunnerLoop`):

- New injected seam `heal_orphans: Callable[[dict, set[str]], Awaitable[list[tuple[str, str]]]]`
  (config, tracked refs) — orchestrator implements it via `asyncio.to_thread`
  over the two helpers, using the shared `_gh_run` seam.
- Called from `_refill_and_sweep` when mode ≠ `off`, throttled to once per
  `_ORPHAN_SWEEP_INTERVAL` (1 h) via the injected monotonic clock.
- Emits `orphan_closed` / `orphan_reclaimed` feed events per healed ref.

Deploying this heals the 22 stuck KinDash issues automatically: merged-PR
orphans get closed, stale ones return to the candidate pool.

## Out of scope

- The `first: 100` search-page cap (KinDash's whole backlog is 25 issues).
- `needs-human` retry (deliberate 7-day untouched-window pool already exists).
- The `no:assignee` gate (intentional design).

## Testing

- `heal_orphan_claims`: fake `gh_run` — merged-PR orphan closed+stripped; open-PR
  orphan untouched; no-PR orphan stripped; tracked refs untouched; per-pass cap.
- `query_claimed_refs`: query construction + parse + failure → `[]`.
- Loop: handoff sets `handed_off` and emits once; merged-after-handoff runs
  close-out; closed-unmerged-after-handoff escalates; no attempt charged on a
  handed-off row; orphan sweep throttle honored; mode off → no heal.
- Orchestrator: `count_in_flight`/`repo_load` exclude handed-off rows.
