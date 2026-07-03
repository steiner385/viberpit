# Insights — Check Health (Slice A)

**Date:** 2026-07-01
**Status:** Approved design, pending implementation plan
**Feature area:** 001-ccswitch-web · Insights (new top-level tab)

## Problem

ccswitch's Pipeline view shows *live* PR/queue state, but nothing about **CI check
quality over time** — which checks are slow, highly variable, or flaky. Plotroom's
Insights view answers this (among ~20 analytics panels). This is the first thin slice of
an Insights port: a per-repo **Check Health** view with two panels, built entirely on
data ccswitch already has.

## Scope

**In scope:** a new **Insights** top-level tab, scoped to one repo (reusing the Pipeline
repo selector), showing two panels — **Slowest / most-variable checks** and **Flakiest
checks** — with a window selector.

**Explicitly out of scope (future slices):** duration regressions, runner-wait health,
queue analytics, cost/DORA/policy/outcomes, the estimator/recommendation engine, and
cross-repo aggregation. This slice establishes the Insights tab + endpoint + store-read
pattern the later slices reuse.

## Decisions (from brainstorming)

1. **Grain:** per-repo, reusing the Pipeline tab's repo selector (matches the read-only
   `HistoryStore` keying of `(repo, check_name, event)`).
2. **Panels (MVP):** Slowest/most-variable + Flakiest only. Regressions and runner-wait
   health deferred.
3. **Window:** a selector — **7d default**, options 24h / 7d / 14d / 30d — applied to both
   panels via `completed_at >= cutoff`.
4. **Nav:** a new top-level tab (not a Pipeline sub-section).
5. **Compute:** on-demand at request time (read-only sqlite, cheap); no caching, no new
   persistence.

## Data source

Everything reads the **already-migrated, read-only** `check_durations` table
(`server/pipeline/history.py` `HistoryStore` over `pipeline_history.db`):

```
check_durations(repo TEXT, check_name TEXT, event TEXT, duration_secs REAL,
                completed_at TEXT /* ISO-8601 */, conclusion TEXT, head_sha TEXT,
                run_attempt INTEGER, started_at TEXT, run_number INTEGER)
```

`completed_at` is an ISO-8601 string; window filtering uses lexicographic `>=` on the ISO
cutoff, matching the existing `expected_set` query. No new tables, no writes.

## Architecture

### Data flow

```
GET /api/pipeline/insights/checks?repo=<r>&window=7d
  └─ orchestrator.check_insights(repo, window)
       └─ HistoryStore.slowest_checks(repo, since_iso, limit) + flakiest_checks(...)
            └─ pure SQL + percentile/flake math over check_durations
  → { repo, window, slowest: [...], flakiest: [...] }
React: useCheckInsights(repo, window) (30s refetch)
  └─ InsightsView → two <CheckTable> panels + window selector
```

### Components & boundaries

| Unit | Purpose | Depends on |
|---|---|---|
| `HistoryStore.slowest_checks` / `flakiest_checks` (read methods) | SQL + aggregation over `check_durations` | sqlite (read-only) |
| `_percentile`/flake helpers | pure math (reuse existing `percentile`) | — |
| `orchestrator.check_insights(repo, window)` | resolve window→cutoff, call the store, assemble payload | HistoryStore |
| `RemoteOrchestrator.check_insights` + RPC handler | daemon-mode proxy (breadth guards) | IPC |
| `GET /api/pipeline/insights/checks` | serve `{repo, window, slowest, flakiest}` | orchestrator, auth |
| `useCheckInsights(repo, window)` hook | fetch + refetch | API |
| `InsightsView` + `CheckTable` + window selector | render the two panels | hook, repo selector |

## Store read methods (pure SQL + math)

Add to `HistoryStore` (`server/pipeline/history.py`); reuse the existing `percentile()`.

**`slowest_checks(repo, since_iso, limit=10, event=None) -> list[CheckDurationStat]`**
- Query SUCCESS rows: `SELECT check_name, event, duration_secs FROM check_durations WHERE
  repo=? AND conclusion='SUCCESS' AND completed_at>=?` (optionally `AND event=?`).
- Group in Python by `(check_name, event)`; for each group with ≥1 sample compute
  `p50 = percentile(sorted, 0.5)`, `p90 = percentile(sorted, 0.9)`,
  `ratio = p90/p50` (or `None` when `p50==0`), `n = len`.
- Return sorted by `p50` desc, top `limit`.
- `CheckDurationStat = {check_name, event, p50, p90, ratio, n}` (frozen dataclass with
  `to_dict`).

**`flakiest_checks(repo, since_iso, limit=10, event=None) -> list[CheckFlakeStat]`**
- Query all rows (any conclusion): `SELECT check_name, event, head_sha, conclusion,
  run_attempt FROM check_durations WHERE repo=? AND completed_at>=?`.
- Group by `(check_name, event, head_sha)`. A commit is **flaky** for that check when its
  rows contain **both** a failure (`conclusion` in `{failure, cancelled, timed_out}`, case-
  insensitive) **and** a success — i.e. the same commit produced different outcomes
  (retry-induced pass; corroborated by `run_attempt > 1` on the success).
- Per `(check_name, event)`: `total_commits = distinct head_sha`,
  `flaky_commits = count of flaky commits`, `flake_pct = 100*flaky/total`,
  `n = total rows`.
- Return sorted by `flake_pct` desc then `flaky_commits` desc, top `limit`; drop checks
  with `total_commits == 0`.
- `CheckFlakeStat = {check_name, event, flake_pct, flaky_commits, total_commits, n}`.

**Conclusion normalization:** lowercase `conclusion` before comparing; treat `success` as
pass, `{failure, cancelled, timed_out, error, action_required}` as fail, and skip
`{skipped, neutral, None, ''}` from both the flake numerator and denominator's outcome
test (a commit with only skipped/neutral runs is not counted as flaky).

## Window handling

`window` query param ∈ `{24h, 7d, 14d, 30d}` (reject others → 400 or clamp to 7d).
`check_insights` maps it to `since_iso = (now - delta).strftime(ISO)` and passes it to both
store methods. `now` is injectable for tests.

## API

`GET /api/pipeline/insights/checks?repo=<nameWithOwner>&window=7d`
→ `{ "repo": "...", "window": "7d", "slowest": [CheckDurationStat...],
     "flakiest": [CheckFlakeStat...] }`
- Depends on `current_identity`; reads the store; **no GitHub call**.
- When the history DB is absent/unavailable (`orch.history_store()` is `None`) or the repo
  has no rows: return empty lists (the frontend shows an empty state), not an error.

## Frontend

- **Nav:** extend `TabId` with `'insights'`; add `/insights` route in `App.tsx`; add the
  tab button in `Dashboard.tsx` (mirror the Pipeline/Automation button + ref pattern).
- **`InsightsView.tsx`:** reads the currently-selected repo (same mechanism the Pipeline
  tab uses) + a local window state (default `7d`); renders a window `<select>` and two
  `<CheckTable>` panels. Empty state ("No check history for <repo> yet") when both lists
  are empty.
- **`CheckTable.tsx`:** a presentational table given a title, columns, and rows — used for
  both panels (slowest columns: check/event/p50/p90/ratio/n; flakiest columns:
  check/event/flake%/flaky·total/n). Durations formatted with the existing duration
  formatter; percentages to 0–1 decimals.
- **`useCheckInsights(repo, window)`** in `hooks.ts`: `QK.pipelineInsights(repo, window)`,
  `staleTime 15_000`, `refetchInterval 30_000`, `enabled: !!repo`.
- TS types `CheckDurationStat`, `CheckFlakeStat` in `api/types.ts`, mirroring the backend
  `to_dict`.

## Error handling

- Missing/empty history DB → empty lists (no 500); frontend empty state.
- Unknown `window` value → clamp to `7d` (defensive; the UI only sends valid values).
- `p50 == 0` (degenerate) → `ratio = None`; the table renders "—".
- Store queries are read-only and wrapped so a malformed row can't 500 the endpoint.

## Testing (TDD)

- **Store methods (heaviest):** build a temp sqlite with the `check_durations` schema,
  insert synthetic rows, open `HistoryStore` over it. Assert `slowest_checks` p50/p90/ratio
  and ranking; assert `flakiest_checks` on: fail-then-pass same sha (flaky), retry-pass
  (`run_attempt 2`, flaky), all-green (0%), all-fail (0% — no success, not "flaky"),
  skipped-only (excluded), and window filtering by `completed_at`.
- **Orchestrator/API:** `check_insights` window→cutoff mapping (injected `now`); route
  shape test with a fake orchestrator (auth via the `test_pipeline_route.py` mechanism);
  `history_store() is None` → empty lists.
- **Daemon breadth:** `check_insights` added to `RemoteOrchestrator` + registered RPC so
  `test_remote_orchestrator_breadth` and `test_orchestrator_rpc_breadth` stay green.
- **Frontend:** `useCheckInsights` hook (mocked `api.get`); `CheckTable` render;
  `InsightsView` renders both panels from a fixture and shows the empty state.
- Gates: `ruff`, `mypy server/ --ignore-missing-imports`, `vitest`, `tsc --noEmit`,
  `eslint` all green. (mypy and the daemon-breadth guards are the two easily-missed gates.)

## Open items resolved during planning

- Exact set of `conclusion` values present in the real `check_durations` data (the
  normalization list above is the working set; verify against a sample during Task 1 and
  widen if needed).
- Whether `event` should be shown as a column or collapsed (kept as a column — a check can
  differ by event, matching the store's keying).
