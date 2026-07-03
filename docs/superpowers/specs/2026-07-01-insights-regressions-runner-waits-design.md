# Insights slice B — Duration Regressions + Runner-Wait Health

**Date:** 2026-07-01 · **Branch:** `insights-regressions-runner-waits` · **Depends on:** slice A (Check Health, PR #386, merged)

Second slice of the Plotroom **Insights** port. Adds two more panels to the existing per-repo
Insights tab, both computed on-demand from data ccswitch already collects — no new collection, no
writes, no new IPC surface.

## Goal

Extend the Insights **Check Health** tab with:
1. **Duration Regressions** — checks whose rolling-median duration has stepped up (a check that got
   slower). Window-independent — "what is regressed *now*".
2. **Runner-Wait Health** — per-event runner pickup wait (`p50`/`p90`) with a delta vs. the prior
   equal-length window.

Both extend the existing `GET /api/pipeline/insights/checks` payload and the `useCheckInsights`
hook; the panels render as two more `CheckTable`s in `InsightsView`.

## Non-goals (deferred)

- **Per-pool starvation alerts** (Plotroom's `PoolHealthView`: last-hour p90 vs 7-day baseline,
  4×/300s enter, 2× clear). Needs a live hourly poller scan ccswitch doesn't run. The
  `runner_waits.pool` column exists, so this is a clean later slice.
- Duration *improvements* (steps down), cost/DORA/policy/estimator panels — later slices or no data.
- A separate `/insights/regressions` endpoint — rejected; extending the one payload keeps it to a
  single authenticated route + one daemon RPC (already breadth-guarded).

## Data sources (already migrated, read-only)

- `check_durations(repo, check_name, event, duration_secs, completed_at, conclusion, head_sha,
  run_attempt, started_at, run_number)` — for regressions (SUCCESS rows).
- `runner_waits(repo, check_name, event, wait_secs, started_at, pool)` — for runner-wait health.

Both live in `~/.claude-accounts/pipeline_history.db`, opened `mode=ro`. The store
(`server/pipeline/history.py`) already has the `percentile()`/`median()` helpers, `recent_durations`,
`expected_runner_wait`, and the slice-A methods `slowest_checks`/`flakiest_checks`.

## Component 1 — `HistoryStore.duration_regressions` (new read method)

Ports Plotroom's `measureDurationStep` (`plotroom/server/estimator/regression.ts`).

Constants (module-level, mirroring Plotroom):
```
_REG_RECENT_K = 10      # most-recent samples → recent p50
_REG_PRIOR_N  = 20      # samples before those → prior p50
_REG_MIN      = 30      # need ≥ 30 SUCCESS samples total
_REG_RATIO    = 1.5     # flag when recent/prior ≥ 1.5
_REG_MIN_DELTA = 60.0   # AND recent − prior ≥ 60s (both guards must trip)
```

Signature: `duration_regressions(self, repo: str) -> list[CheckRegressionStat]`
(window-independent — no `since` param).

Algorithm:
1. `SELECT DISTINCT check_name, event FROM check_durations WHERE repo=?` — enumerate check/event pairs.
2. For each pair, `SELECT duration_secs, completed_at FROM check_durations WHERE repo=? AND
   check_name=? AND event=? AND conclusion='SUCCESS' ORDER BY completed_at DESC LIMIT 30`
   (exact-uppercase `'SUCCESS'`, matching the slice-A / `recent_durations` convention).
3. If `< _REG_MIN` rows → skip. Else `recent = median(first 10 durations)`,
   `prior = median(durations[10:30])`. If `prior <= 0` → skip.
4. Flag when `recent/prior ≥ _REG_RATIO` **and** `recent − prior ≥ _REG_MIN_DELTA`.
5. `since` = `completed_at` of the **oldest** row in the recent-10 window (`rows[9].completed_at`) —
   the approximate onset.
6. Return flagged `CheckRegressionStat(check_name, event, prior_p50, recent_p50, ratio, since)`,
   sorted by `ratio` desc.

`CheckRegressionStat` — frozen dataclass with `to_dict()`:
`{check_name: str, event: str, prior_p50: float, recent_p50: float, ratio: float, since: str}`.

Notes: ~1 tiny `LIMIT 30` indexed query per (check,event) pair rather than pulling all SUCCESS rows —
bounded and cheap for the ~dozens of checks per repo. No hysteresis/clear-ratio state (that is
Plotroom's *live cache* concern; the on-demand recompute is stateless and re-evaluates every call).

## Component 2 — `HistoryStore.runner_wait_health` (new read method)

Ports Plotroom's per-`(repo,event)` runner-wait block (`plotroom/server/metrics.ts` §1).

Signature: `runner_wait_health(self, repo: str, since_iso: str, prev_since_iso: str)
-> list[RunnerWaitStat]`.

Algorithm:
1. `SELECT event, wait_secs, started_at FROM runner_waits WHERE repo=? AND started_at >= ?`
   with `prev_since_iso` — one read covering both windows.
2. Split per row: **current** = `started_at >= since_iso`; **prior** = `prev_since_iso <=
   started_at < since_iso`.
3. Group by `event`. For each event with current rows: `p50`/`p90` over current `wait_secs` (via
   `percentile(sorted, 0.5)` / `percentile(sorted, 0.9)`), `prev_p50` = `percentile(sorted prior,
   0.5)` (same helper as the current p50 — consistent with slice A; `None` when no prior rows),
   `n` = current sample count.
4. Return `RunnerWaitStat(event, p50, p90, prev_p50, n)` sorted by `event`.

`RunnerWaitStat` — frozen dataclass with `to_dict()`:
`{event: str, p50: float, p90: float, prev_p50: float | null, n: int}`.

## Component 3 — `orchestrator.check_insights` (extend existing)

The existing method maps the window to `since_iso`. Extend it to also:
- compute `prev_since_iso = (now − 2×window).strftime(...)` (same ISO-`Z` format used today);
- call `store.duration_regressions(repo)` and `store.runner_wait_health(repo, since_iso,
  prev_since_iso)` inside the existing `try/except Exception` (still returns empty lists on any
  failure — never raises);
- add `"regressions": [r.to_dict() ...]` and `"runner_wait_health": [w.to_dict() ...]` to the
  returned dict, and to the empty-payload early-returns (`store is None` / no repo).

No change to `RemoteOrchestrator.check_insights` or the RPC handler — the payload is still a plain
JSON-safe dict passed through untouched, so the daemon proxy and breadth guards already cover it.

## Component 4 — Frontend

- `web/src/api/types.ts`: add `CheckRegressionStat` and `RunnerWaitStat` interfaces (mirror the
  `to_dict` shapes, `prev_p50: number | null`); extend `CheckInsights` with `regressions:
  CheckRegressionStat[]` and `runner_wait_health: RunnerWaitStat[]`.
- `web/src/components/InsightsView.tsx`: two more `CheckTable`s after the existing two.
  - **Duration regressions** — columns: Check · Event · Was (prior p50, `fmtSecs`) · Now (recent p50)
    · Slowdown (`ratio.toFixed(1)×`) · Since (`since` date). Header carries a
    *"live — not affected by window"* note. Empty: *"No regressions detected"*.
  - **Runner-wait health** — columns: Event · p50 (`fmtSecs`) · p90 · Δ vs prev (`prev_p50` → arrow +
    signed delta, "—" when `prev_p50` null) · n. Empty: *"No runner-wait data"*.
- Reuse the existing `fmtSecs` formatter and `CheckTable<T>` generic. No hook change beyond the type
  (the same `useCheckInsights(repo, window)` now returns the extra arrays).

## Testing

- **Store — `duration_regressions`** (heaviest): a check with a genuine step (prior ~30s, recent
  ~120s → flagged, ratio≈4, correct `since`); a check under the ratio threshold (not flagged); a
  check over ratio but under the +60s absolute floor (not flagged); a check with < 30 samples (not
  flagged); ordering by ratio desc.
- **Store — `runner_wait_health`**: per-event p50/p90 correctness; prev-window split boundary
  (a row exactly at `since_iso` counts as current, one just before as prior); `prev_p50` null when
  no prior rows; event filtering/sorting.
- **Orchestrator**: window → `since_iso` + `prev_since_iso` (= now − 2×window) with injected `now`;
  new keys present; empty lists when store is None (never raises).
- **Route**: 200 payload now carries `regressions` + `runner_wait_health`; 401 without identity.
  (Daemon breadth guards already green — no new method to register, but re-run them.)
- **Frontend**: hook type, the two new `CheckTable`s render + empty states, and the InsightsView test
  updated for the added panels. Full `vitest`/`tsc`/`eslint` green.

Gates (CI-authoritative, verified locally): `ruff`, `mypy server/ --ignore-missing-imports`,
feature + breadth `pytest`, `vitest`, `tsc --noEmit`, `eslint`.

## Follow-ups (non-blocking)

- Per-pool starvation panel (the deferred `runner_waits.pool` breakdown + live scan).
- The slice-A a11y gap (`role="tabpanel"`/`id` on the panel div) — sweep across all tabs in one pass.
