# CI ETA Engine (Plotroom estimator port) — Design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Program:** ccswitch⨉Plotroom combined-app. This is **M5 slice 1c** (ETA engine), unblocked.
Supersedes the "BLOCKED — do not start" stance of
`docs/superpowers/plans/2026-06-24-m5-1c-eta-engine.md` (see §1).

---

## 1. Why this is no longer blocked

The 1c plan was blocked on a **live-parity surface**: it needed a PR currently exposing an ETA
in the running Plotroom oracle (`http://127.0.0.1:4400/api/state`) to diff against, and on
2026-06-24 there were **0** such PRs.

As of 2026-06-25 the gate is **open**. The live Plotroom watches two owners
(`owners: ['cairnea','steiner385']`) and currently exposes three PRs with non-null
`stage.etaSeconds`:

- `cairnea/KinDash` #10647 → `etaSeconds: 724, percent: 46, overdue: false` (all checks COMPLETED)
- `cairnea/KinDash` #10657 → `etaSeconds: 731, percent: 42, overdue: false` (all checks COMPLETED)
- `steiner385/cc-account-switcher` #293 → `etaSeconds: 315, percent: 0` (0 live checks)

No operational change to the running service is required.

**Favorable parity properties discovered while scoping:**
- Plotroom `config.json` has `repos: {}` — **no per-repo required-prefix / rollup-workflow config**.
  So `effectivePrefixes(repo) → []`, `rollupWorkflowFor(repo) → null`, and `workflowScopeAllows`
  is always true. The `expectedSet` filter collapses to the live `isRequired`-marking branch.
- The KinDash ETA PRs have **all live checks COMPLETED**; their ETA derives entirely from
  history-expected-but-absent checks (`rem = p50 + queueDelay`), which is **independent of `now`**.
  So parity is stable across the clock-skew between the oracle's snapshot and ccswitch's compute run.

---

## 2. Scope & non-goals

**In scope — the CI-progress ETA, end to end:**
1. Migrate the real history tables into a ccswitch-owned store.
2. Port `progress.ts` (`computeProgress`) and the history read-helpers it needs.
3. Port the required-checks / expectedSet glue (collapses to the no-config path, ported faithfully).
4. Extend the PR GraphQL to fetch per-check timing (`status`, `startedAt`, `isRequired`).
5. Compute `eta_seconds` / `percent` / `overdue` per PR; expose on `PullRequestInfo`; flow through
   the existing `all_prs()` / `GET /api/prs` path.
6. Render the ETA on `ci`-stage rows of the Pipeline board.
7. **Live-parity gate**: diff ccswitch's computed ETA against the oracle's `etaSeconds` for the
   three PRs above, within the existing float tolerance.

**Explicit non-goals (deliberately cut):**
- **Queue-stage ETA** (`queue.ts` / `queueStage`) — merge-queue parity is blocked (no merge queue
  on these repos). Parity targets ETAs that derive from CI progress only.
- **Ongoing history recording** — we consume a migrated *snapshot*; we do NOT port `waits.ts`
  (`extractRunnerWaits`/`classifyWait`) or the workflow needs-graph, because the runner-pickup
  medians are read directly from the migrated `runner_waits` table. Keeping the snapshot fresh
  (re-deriving durations from observed runs) is the named follow-up capability.
- **`etaRangeSeconds`** (wide-variance band) — `compute_progress` may return it, but rendering the
  band is deferred; only the point ETA is displayed.
- **Deploy / qa-live / prod-live ETAs.**
- **`progress.failed`** — ccswitch already derives `checks: 'failing'`; the estimator's `failed`
  output is ignored (not a second source of truth).

---

## 3. Architecture — components

Each unit has one responsibility, a clear interface, and is independently testable.

### 3.1 `pipeline_history.db` + migration — `server/pipeline/history_migrate.py`
Schema-faithful copy of `check_durations` and `runner_waits` from Plotroom's `history.db`
(default source path `/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db`, opened
`file:…?mode=ro` — it is WAL and live-written; **never write to it**) into a **separate** ccswitch
SQLite `pipeline_history.db` (ADR-1: not the main app DB; `create_all`/`init_db` never touch it).
Idempotent (re-run replaces rows). Verified by per-table row-count match against the source.

Source schemas (verbatim columns to preserve):
- `check_durations`: `repo, check_name, event, duration_secs, completed_at, conclusion, head_sha,
  run_attempt, started_at, run_number`
- `runner_waits`: `repo, check_name, event, wait_secs, started_at, pool`

### 3.2 History read-helpers — `server/pipeline/history.py`
Pure SQL over `pipeline_history.db`, mirroring Plotroom's `history.ts` semantics:
- `recent_durations(repo, name, event) -> list[float]` — SUCCESS durations, newest-first, recency
  filtered: prefer samples within `DURATION_FRESH_DAYS` (14) of the job's own newest sample; fall
  back to the last `DURATION_FALLBACK_LIMIT` any-age when fewer than `DURATION_FRESH_MIN` are fresh.
- `expected(repo, name, event) -> Expected | None` — `{p10, p50, p90, n}` (None when no samples).
- `samples(repo, name, event) -> list[float]` — alias of `recent_durations`.
- `expected_set(repo, event, now, window_days=14) -> list[str]` — canonical check names seen for
  `(repo, event)` within the window.
- `expected_runner_wait(repo, name, event) -> float | None` — median of `runner_waits.wait_secs`.
- `median(xs)`, `percentile(sorted_xs, q)` helpers (match Plotroom's `math.ts` interpolation).

Copy the exact constants from `history.ts`: `DURATION_FRESH_DAYS`, `DURATION_FRESH_MIN`,
`DURATION_FALLBACK_LIMIT`.

### 3.3 Estimator — `server/pipeline/estimator.py`
Faithful port of `compute_progress` (`progress.ts`, 113 lines) → a pure
`compute_progress(checks, expected_set, lookup, now, samples=None, queue_delay=None) -> ProgressResult`
where `ProgressResult = {percent, eta_seconds, overdue}` (drop `failed`/`etaRangeSeconds` from the
consumed result; they may be computed internally but are not surfaced). Preserve verbatim:
- constants `RUNNING_CAP=0.97`, `OVERDUE_FACTOR=1.5`, `DEFAULT_DURATION=60`;
- the bimodal **conditional-remaining** re-anchoring (≥5 raw samples → filter to samples `> elapsed`;
  ≥2 qualifying → median/p90 of qualifying minus elapsed; the ≥10-vs-5–9 overdue split);
- COMPLETED (weight 1) / IN_PROGRESS-with-`startedAt` / queued-or-expected-absent branches;
- SKIPPED skipped; non-parseable `startedAt` treated as queued;
- the weighted-progress aggregate and the final
  `!anyUnfinished → 100/0`, `overdue → null eta`, else `round(etaP50)` returns.

### 3.4 Required-checks glue — `server/pipeline/required.py`
Port from `poller.ts` + `classify.ts`, collapsing to the no-config path but faithfully:
- `effective_prefixes(repo) -> list[str]` → `[]` (no per-repo config in ccswitch).
- `rollup_workflow_for(repo) -> str | None` → `None`.
- `workflow_scope_allows(check_workflow, rollup_workflow) -> bool` → `True` when rollup is None.
- `required_checks(checks, prefixes, rollup_wf) -> list[Check]` — with `prefixes=[]`/`rollup_wf=None`,
  reduces to the `isRequired`-marked subset (or all checks when none are marked — match Plotroom).
- `build_expected_set(history_expected_set, checks, prefixes, rollup_wf, now)` — the `poller.ts`
  expectedSet branch: prefixes path (skipped, none), else `hasRequiredMarking → filter to
  requiredNames`, else the all-done-stale path (`EXPECTED_SET_STALE_MS`).

### 3.5 Per-check timing in the PR query — `server/github/pr_status.py`
Extend the shared `_PR_NODE_FIELDS` (or a parallel population field set) so each rollup context
yields `status`, `startedAt`, and PR-scoped `isRequired` in addition to `name`/`conclusion`. A new
parser maps these to the estimator's `checks` shape: `{name, status, conclusion, startedAt,
isRequired, workflowName, event}` (event fixed to `'pull_request'` for the CI-progress path, matching
Plotroom). The exact GraphQL for `isRequired` (it takes a `pullRequestNumber`/`pullRequestId`
argument on `CheckRun`/`StatusContext`) is pinned in the implementation plan.

### 3.6 Compute + wire
A small enrichment computes the ETA per open PR (open-stage only; merged/closed get no ETA) by
assembling §3.2 history + §3.4 expected-set + §3.5 live checks and calling §3.3. It adds three
additive fields to `PullRequestInfo`:
- `eta_seconds: int | None`, `eta_percent: int | None`, `eta_overdue: bool` (default None/None/False).
Update the single `to_dict`/`from_dict` pair (RPC-stable; tolerant of old payloads) and the frontend
type. Values flow unchanged through `all_prs()` / `prs_for()` / `GET /api/prs`.

### 3.7 Board display — `web/src/components/PipelineView.tsx` (+ `PrPill` if cleaner)
On `ci`-stage rows, render the ETA: `~Xm` (humanized from `eta_seconds`), or `overdue` when
`eta_overdue`, or nothing when `eta_seconds == null`. Optionally a faint percent. Depends on #302's
`PipelineBoard`; this branch bases on #302 (or `origin/master` once #302 merges).

### 3.8 Live-parity gate — `server/github/parity/`
Extend `coverage.py` `PHASE_FIELDS` with `eta_seconds` under float tolerance (±max(2s, 5%), already
specced in `normalize.py`). The harness computes ccswitch's ETA from migrated history + live checks
for each oracle PR exposing an `etaSeconds`, and asserts no mismatch for #10647/#10657/#293.
Restrict the parity comparison to CI-progress ETAs (queue-stage ETAs are out of scope).

---

## 4. Data flow

```
Plotroom history.db (ro) ──migrate──▶ pipeline_history.db ──▶ history.py (expected/samples/
                                                              expected_set/runner_wait)
                                                                    │
live PR checks (status/startedAt/isRequired via gh GraphQL) ───────┤
                                                                    ▼
                              required.py (expectedSet/requiredChecks) + estimator.compute_progress()
                                                                    │  (eta_seconds, percent, overdue)
                                                                    ▼
                          PullRequestInfo ──▶ all_prs()/prs_for ──▶ GET /api/prs ──▶ PipelineBoard (ci rows)

live oracle /api/state etaSeconds ──────────────────────────────▶ parity harness diff (GATE)
```

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| **Crown-jewel drift** — subtle divergence from Plotroom's tuned output | Faithful line-by-line port + the **live-parity gate** (§3.8) on three real PRs; the conservative concern that "unit tests alone miss divergence" is answered by live parity. |
| **`isRequired` GraphQL** — needs a PR-scoped argument; missing it breaks the expectedSet filter for repos with required markings (KinDash) | The plan pins the exact `isRequired(pullRequestNumber:)` query; a parity miss on #10647/#10657 catches it. |
| **Snapshot staleness** | Accepted for v1; ccswitch reads from the same history Plotroom wrote, so p50/p90 match within tolerance. Ongoing recording is the named follow-up. |
| **#302 dependency** (board display) | Base this branch on #302; fall back to `origin/master` if #302 merges first. Backend units (§3.1–3.6, 3.8) are independent of #302. |
| **Scope** — largest port yet | Tight unit boundaries (§3) → many small TDD tasks; queue/recording/range cut as non-goals (§2). |

---

## 6. Success criteria

- Live-parity harness reports **0 mismatches** on `eta_seconds` for the oracle's CI-progress ETA PRs
  (#10647, #10657, #293) within ±max(2s, 5%).
- `compute_progress` unit tests cover bimodal re-anchoring, the overdue splits, `<5`-samples→no-ETA,
  and all-complete→`(100, 0, false)`.
- Migration row counts match the source for `check_durations` and `runner_waits`.
- The Pipeline board shows an ETA on `ci`-stage PRs that have one, and nothing (not a fabricated 0)
  when the estimator returns `None`.
