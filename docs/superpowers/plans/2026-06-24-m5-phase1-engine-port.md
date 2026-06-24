# M5 Phase 1 — Engine Port (stage machine + ETA engine), parity-gated

> Decomposition plan. Each slice is its own subagent-driven build, gated by the Phase-0 parity harness (`server/github/parity/`, working PARITY-OK gate as of #258). The rule for every slice: **port one capability → widen `PHASE_FIELDS` with its field(s) → live-parity must stay green (zero mismatches on the shared PRs) before cutover.**

**Goal:** Re-express Plotroom's per-PR pipeline engine in ccswitch's Python backend so the unified app computes stage + ETA + queue/deploy signals itself — unlocking the stall/ship signals (M4's remaining piece) and the Pipeline view, and eventually retiring Plotroom.

**Source (read-only reference):** `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/plotroom/557e6b2f/server/` — the stage machine is in `poller.ts` (`calibrateStage` + the rawStage derivation; stage values `ci | ready | queue | parked(+substate) | qa-deploy | awaiting-prod | prod | merged`), the ETA engine is `estimator/*.ts` (~21 files: `progress`, `waits`, `regression`, `starvation`, `queue-health`, `deploy-status`, `critical-path`, `calibrate`, `flake-summary`, …), the workflow `needs:`-graph is `pipeline-model/`, deploy ancestry is `ancestry.ts`, learned history is `history.ts`.

**Hard constraints (carried from the program):**
- **Parity-gate every slice.** Never declare a slice done until `python -m server.github.parity` shows zero mismatches on its newly-covered field against live Plotroom. Never loosen `PHASE_FIELDS` to pass.
- **Port pure functions as pure functions** (history rows + PR facts in → value out), so each is unit-testable and parity-diffable without DB/network — mirroring how `pr_status` classification is already reused by the candidate.
- **History continuity:** the estimator is only accurate with Plotroom's learned history — slice 1c migrates `history.db` (`check_durations`, `queue_waits`, `runner_waits`, `eta_accuracy`, `group_runs`, `observed_pools`) into ccswitch's engine store before ETAs are parity-gated, else ETAs cold-start.
- **Candidate must produce the new field too.** Each slice extends `compute_candidate_by_number` (or the engine that backs it) to emit the field, classified by the *same* logic Plotroom uses, so a mismatch is a real divergence.
- Backend tests via the clone venv + `PYTHONPATH=$PWD/src`; whole-tree ruff/eslint; subagent-driven per task.

---

## Slice ordering (each its own PR, parity-gated)

### 1a — Stage classifier: the open stages `{ci, ready, parked(+substate)}`
The cheapest, highest-value first slice — these stages derive from data ccswitch already has (PR checks + `mergeStateStatus` + `isDraft`), no queue/deploy/history needed.
- **Port:** a pure `derive_open_stage(checks, merge_state, is_draft, required_failed) -> (stage, substate)` matching Plotroom's rawStage logic for the open stages: `parked/draft` (draft), `parked/conflicting` (mergeStateStatus CONFLICTING), `parked/ci-failed` (a required check failed), `ci` (checks running), `ready` (checks green, mergeable). Read Plotroom `poller.ts` rawStage + `parked` substate logic for the exact predicates.
- **Oracle:** `normalize.py` already has `pr.stage.stage`; extract `stage` + `substate` into oracle fields.
- **Candidate:** extend the GraphQL PR-list query with `isDraft` + `mergeStateStatus` + the rollup; compute `stage`/`substate` via the port.
- **Gate:** `PHASE_FIELDS[1] = PHASE_FIELDS[0] | {stage}` (and `substate` once stable); live-parity green on `stage` for open PRs.
- **Unlocks:** richer triage/dispatch (real parked/ci-failed signal) immediately, even before queue/deploy.

### 1b — Queue stage + position
- Port the merge-queue stage (`queue`), `queueAheadCount` (`behind N`), and group-failed substate from `poller.ts` + `trains.ts`. Needs merge-queue group data from GitHub.
- Gate: widen with `stage=queue` + `queue_ahead`. **Unlocks M4's stall (`queue-blocked`) push.**

### 1c — ETA engine foundation + history migration
- Migrate `history.db` → ccswitch engine store (schema-faithful copy; verify row counts). Port `estimator/progress.ts` (conditional-remaining estimator) + `waits.ts` (runner pickup-wait) + `calibrate.ts`.
- Gate: widen with `eta_secs`/`overdue` using the `±max(2s,5%)` float tolerance; backfill-verification test (post-migration ETAs match pre-migration Plotroom).

### 1d — Deploy stages + ancestry
- Port `ancestry.ts` (compare-API deploy ancestry, api mode) + the `qa-deploy`/`awaiting-prod`/`prod`/`shipped` stages + `deploy-status.ts`.
- Gate: widen with the deploy stages. **Unlocks M4's ship (`prod-live`) push.**

### 1e — Remaining estimator signals
- `regression.ts`, `starvation.ts`, `queue-health.ts`, `flake-summary.ts`, `critical-path.ts` — each ported + parity-checked where it surfaces a comparable field; otherwise unit-tested against recorded history.

*(Frontend Pipeline view = Phase 3; decommission Plotroom = Phase 4. Out of scope here.)*

---

## Start here: Slice 1a is the next executable unit
Read `poller.ts`'s rawStage + parked-substate derivation, port `derive_open_stage` (pure, unit-tested), extend the parity candidate + oracle with `stage`, widen `PHASE_FIELDS[1]`, and confirm live-parity green on `stage`. It depends only on the merged parity harness (#258) + data foundation already in place.

## Honest scope note
This is a multi-week program even with subagent parallelism — the value is stall/ship signals + the unified Pipeline view, incremental over the automations (M1–M3) already shipped. Each slice lands independently and parity-gated, so it can be paused at any slice boundary with a working app.
