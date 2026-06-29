# M-Track-6: ETA Range (lower / nominal / upper) — Design

**Date:** 2026-06-29
**Status:** Approved (design); pending user review → implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. Sixth milestone, building
on M-Track-1 (stage track) and M-Track-5 (per-check drill-down, PR #349; prod-resilience fix #351).

---

## 1. Goal

Today ccswitch shows a **single** CI ETA — one `~Xm` point estimate on the active CI stage node — and
the per-check drill-down rows show only a *fixed coarse* in-flight bar. This milestone restores
Plotroom's **uncertainty range**: an optimistic / nominal / pessimistic forecast rendered as a
**confidence band** in the progress bar — a solid "elapsed" fill, a faded band spanning
**lower→upper**, and a **dark line at the nominal** point.

The range is shown in **two places** (both confirmed during brainstorming):
- **PR CI stage node** — the connector leaving the active CI node becomes the band track ("node
  treatment 2").
- **Per running job** — each running check row in the drill-down renders the same band ("geometry A").

## 2. Key facts that shrink this milestone

- The estimator **already computes** per-check `rem50`/`rem90` and aggregates them (`max`) for the PR
  number (`server/pipeline/estimator.py` `compute_progress`, lines ~99–100), then **discards `eta_p90`**
  and returns only `round(eta_p50)` (line ~107). "Nominal" and "upper" are largely already there.
- `Expected` (`server/pipeline/history.py`) **already stores `p10`, `p50`, `p90`** (and `n`). So the
  optimistic bound is `p10` — no new percentile machinery, no new GitHub data.
- So this milestone is mostly: **stop discarding the bounds**, thread three numbers through the wire,
  and render one band geometry in two mount points.

## 3. Non-goals

- **No new GitHub data / no new query.** `p10/p50/p90` come from the existing history store.
- **No change to the cost line, overdue logic, or stage placement** (`track.py` untouched).
- **No range without history.** When there is no history store (or no `Expected` for a check), the
  range is absent — exactly the graceful path M-Track-5's #351 established. The drill-down rows still
  render (duration/status/cost); they just have no band. ETAs/ranges need `pipeline_history.db` (now
  present in prod, activates on next daemon restart).
- **No new percentiles beyond p10/p50/p90.** YAGNI.

## 4. Architecture — the shared spine (constitution VI)

The estimator's inner per-check remaining-time logic and the drill-down's per-row range MUST NOT be two
implementations. Extract the per-check computation into **one pure helper** that both call.

### 4.1 `check_remaining` — new pure helper

New `server/pipeline/check_eta.py` (pure, no I/O):

```
def check_remaining(
    check: dict | None,          # the live check (status/started_at/conclusion), or None if expected-only
    expected: Expected | None,   # p10/p50/p90 for this check, or None (no history)
    samples: list[float],        # recent durations for this check (may be empty)
    now: datetime,
    queue_delay: float | None = None,
) -> CheckRemaining | None
```
returns `CheckRemaining(rem_low, rem_nominal, rem_high)` (seconds remaining, each ≥ 0), or `None` when
no estimate is possible (no `expected` AND no usable fallback, or the check is COMPLETED/SKIPPED).

The body is the **existing** estimator inner-loop logic, generalised to three bounds:
- **COMPLETED / SKIPPED** → `None` (no remaining).
- **IN_PROGRESS** with `started_at` → `elapsed = now − started`; remaining bounds from the sample-aware
  rule already in `compute_progress` (≥10 samples exceeded ⇒ overdue tail; ≥2 qualifying ⇒
  `median/percentile(qualifying) − elapsed`; else `p10/p50/p90 − elapsed`, floored at 0). Adds the
  `p10` arm alongside the existing `p50`/`p90` arms.
- **QUEUED / expected-but-absent** → `p10/p50/p90 + queue_delay`.

`Expected.p10` is the optimistic bound; the legacy fallback (`fallback = median(known_p50s)`) applies to
all three when a check has no `Expected`.

### 4.2 Estimator consumes the helper

`compute_progress` is refactored to call `check_remaining` per check and aggregate **each** bound with the
existing `max`: `eta_low = max(rem_low…)`, `eta_nominal = max(rem_nominal…)`, `eta_high = max(rem_high…)`.
`ProgressResult` gains `eta_low_seconds` / `eta_high_seconds` (keep `eta_seconds` = nominal, back-compat):

```
@dataclass(frozen=True)
class ProgressResult:
    percent: int
    eta_seconds: int | None        # nominal (p50) — unchanged meaning
    eta_low_seconds: int | None     # optimistic (p10)
    eta_high_seconds: int | None    # pessimistic (p90)
    overdue: bool
```
`overdue`/`percent` semantics unchanged. The overdue short-circuit (`return …None…True`) sets all three
ETA fields to `None` (range meaningless once overdue) — preserves today's behavior.

`compute_pr_eta` (`server/pipeline/eta.py`) returns the triple (+ percent + overdue).

## 5. Architecture — backend wire

### 5.1 PR-level (`PullRequestInfo`)
Add `eta_low_seconds: int | None = None`, `eta_high_seconds: int | None = None` (nominal stays
`eta_seconds`). Additive `to_dict`/`from_dict`. Populated in `_enrich_with_eta` from the new
`compute_pr_eta` triple **only when a store is present** (unchanged gating — ETA needs history; #351).

### 5.2 Per-check (`CheckRow`)
Add `rem_low_seconds`, `rem_nominal_seconds`, `rem_high_seconds` (`int | None`, default `None`; non-null
only for running/queued rows with an estimate). Additive serializers.

`build_check_groups` (`server/pipeline/checkgroups.py`) computes these per row via `check_remaining`,
using `store.expected(repo, name, event)` and `store.samples(...)` — **store-tolerant**: when `store is
None`, the three rem fields are `None` (the row still builds; this is the #351 invariant). `now` is the
already-injected enrichment clock.

> **Dual-serializer / exact-shape traps (project memory):** new `PullRequestInfo` + `CheckRow` wire
> fields touch `to_dict`/`from_dict` and the exact-dict assert in `test_sessions_pr_field.py`
> (memories: `pullrequestinfo-todict-exact-shape-test`, `session-record-dual-serializer`).

## 6. Architecture — frontend (one geometry, two mounts)

### 6.1 Shared band geometry
New `web/src/lib/etaBand.ts`: pure
```
bandSegments({ elapsedS, lowS, nominalS, highS }) ->
  { solidPct, bandLeftPct, bandWidthPct, tickPct } | null
```
where completion times are `elapsed + rem*`; the track scale is `0 .. (elapsed + remHigh)` (per-row
normalisation, matching the mockup). `solidPct = elapsed/scale`; band spans
`(elapsed+remLow)/scale .. 1.0`; tick at `(elapsed+remNominal)/scale`. Clamps to [0,1]; handles
`elapsed > nominal` (tick rides at/at-most the band start). Returns `null` when any bound is absent.

### 6.2 StageTrack — connector-as-track
When the CI node is `active` and the PR carries a range, the **outgoing connector** from the CI node
renders the band (style A) instead of the flat line, driven by `bandSegments`. No range → today's flat
connector + (optional) `~Xm` label. (Reuses `checkStateColor` from `lib/checkState.ts`.)

### 6.3 CheckRowList — per-row band
A running row with rem bounds renders the style-A band via the same `bandSegments`; done rows stay solid
(100%); queued rows render the band from 0. Replaces the current fixed coarse `40%` in-flight fill (the
documented M5 placeholder this milestone supersedes). Right-label stays `elapsed / ~nominal`.

### 6.4 Types
`web/src/api/types.ts`: add `eta_low_seconds?/eta_high_seconds?` to `PullRequestInfo`; add the three
`rem_*_seconds?` to `CheckRow`.

## 7. Data flow

```
Expected.p10/p50/p90 + samples (history store)
  └─ check_remaining(check, expected, samples, now) → (rem_low, rem_nominal, rem_high)
       ├─ compute_progress: max over checks → ProgressResult(eta_seconds, eta_low, eta_high, …)
       │     → compute_pr_eta → PullRequestInfo.eta_{low,high}_seconds  (store-gated)
       └─ build_check_groups: per running row → CheckRow.rem_{low,nominal,high}_seconds  (store-tolerant)
  → to_dict → GET /api/prs
  → StageTrack connector band (CI active) + CheckRowList per-row band  (via lib/etaBand.ts)
```

## 8. Error handling & edge cases

- **No history store / no `Expected` for a check** → all range fields `None`; connector stays flat, rows
  render bandless. Never crashes (the #351 invariant; `check_remaining` returns `None`).
- **overdue** → ETA range `None` (as today); connector shows "overdue", no band.
- **`elapsed > nominal` (running long)** → band still renders; nominal tick clamps to the band's start;
  optimistic completion is `≤ elapsed` so `remLow` floors at 0 (band starts at "now").
- **COMPLETED/SKIPPED rows** → no rem fields, solid bar.
- **Timezone:** `check_remaining` uses tz-aware `now` (injected) and parses ISO with the existing
  `Z→+00:00` handling. No naive `utcnow` (memory: `naive-utc-isoformat-0s-ago`).

## 9. Testing (TDD)

**Backend**
- `check_remaining`: completed/skipped → None; running with p10/p50/p90 → three remainings; running with
  ≥2 qualifying samples → percentile-based; running no history → fallback or None; queued → bounds +
  queue_delay; `elapsed > p90` floors at 0.
- `compute_progress`: aggregates each bound via max; `eta_low ≤ eta_seconds ≤ eta_high`; overdue → all
  three None; existing percent/eta_seconds tests stay green (nominal unchanged).
- `ProgressResult`/`compute_pr_eta` return the triple.
- `PullRequestInfo` + `CheckRow` `to_dict`/`from_dict` round-trip incl. new fields; real `GET /api/prs`
  carries them; update `test_sessions_pr_field.py` exact-shape.
- `build_check_groups`: running row gets rem triple from store; `store=None` → rem fields None (no crash).
- Full backend suite: no new failures vs baseline (`worktree-fullsuite-baseline-diff`).

**Frontend (vitest)**
- `bandSegments`: segment math; clamping; `elapsed>nominal`; null when a bound missing.
- `StageTrack`: CI active + range → connector renders band (testable segment widths); no range → flat
  connector unchanged (existing StageTrack tests green).
- `CheckRowList`: running row renders band; done solid; queued from 0.
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0`.

## 10. Files touched (summary)

**New**
- `server/pipeline/check_eta.py` — `check_remaining` + `CheckRemaining`.
- `web/src/lib/etaBand.ts` — `bandSegments`.
- Tests for each.

**Modified**
- `server/pipeline/estimator.py` — consume `check_remaining`; `ProgressResult` gains low/high.
- `server/pipeline/eta.py` — `compute_pr_eta` returns the triple.
- `server/github/pr_status.py` — `PullRequestInfo` + `CheckRow` new fields + serializers.
- `server/pipeline/checkgroups.py` — per-row rem via `check_remaining` (store-tolerant).
- `server/github/pr_population.py` — thread the triple into `eta_*` fields.
- `web/src/api/types.ts` — new optional fields.
- `web/src/components/StageTrack.tsx` — connector-as-band when CI active.
- `web/src/components/CheckRowList.tsx` — per-row band (replaces fixed 40%).
- `server/tests/test_sessions_pr_field.py` — exact-shape update.

## 11. Risks

- **Estimator refactor regressing the nominal/percent.** Mitigation: `check_remaining` is the *same*
  logic extracted; nominal-path tests (existing `test_pipeline_eta`/estimator tests) must stay green —
  treat any change to `eta_seconds`/`percent` on existing fixtures as a regression.
- **Dual serializer / exact-shape tests** (two structs gain fields) — assert on real responses; update
  the exact-dict PR test (memories above).
- **Bar geometry confusion** if a bound is missing on only some rows — `bandSegments` returns `null` →
  render the prior bandless bar; never a half-drawn band.
- **Band on the tiny connector** — keep it ≥ the existing connector width; verify legibility at the real
  node spacing (the mockup used 60px).
