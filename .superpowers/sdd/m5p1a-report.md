# M5 Phase 1a Report — open-stage classifier, parity-gated on `stage`

## Status
COMPLETE. All gates green; parity OK on live Plotroom.

## Commit SHAs (branch `feat/m5-phase1a-stage`)
1. `2cd9d4c` — feat(pipeline): open-stage classifier derive_open_stage (Phase 1a)
2. `56ba8f2` — feat(parity): add stage field to PullRequestInfo + Phase 1 coverage
3. `8e4860a` — feat(parity): wire candidate stage + oracle stage extraction (Phase 1a)

## Files Created / Modified
- `server/pipeline/__init__.py` (new, empty)
- `server/pipeline/stage.py` (new) — `derive_open_stage(checks, merge_state_status, is_draft) -> str`
- `server/tests/test_pipeline_stage.py` (new) — 10 unit tests for all decision branches
- `server/github/pr_status.py` — additive `stage: str = ""` field with to_dict/from_dict round-trip
- `server/github/parity/coverage.py` — `PHASE_FIELDS[1] = PHASE_FIELDS[0] | frozenset({"stage"})`
- `server/github/parity/normalize.py` — `_OPEN_STAGE_MAP`; extracts `stage` into OraclePr.fields only for ci/ready/parked
- `server/github/parity/__main__.py` — `_pr_list_query` requests `isDraft`+`mergeStateStatus`; `compute_candidate_by_number` calls `derive_open_stage`
- `server/tests/test_pr_status.py` — added `test_stage_field_roundtrips`
- `server/tests/test_parity_coverage.py` — added phase 1 tests
- `server/tests/test_parity_normalize.py` — added `test_normalize_emits_stage_for_open_stages`
- `server/tests/test_parity_cli.py` — added `test_compute_candidate_sets_stage_from_checks_and_merge_state`

## Test / Lint Summary
```
47 passed in 0.14s
ruff: All checks passed!
mypy: Success: no issues found in 8 source files
```

## Live Parity Result (Phase 1, `stage`)
```
matched: 4
mismatches: 0
oracle-only: 222  candidate-only: 0
PARITY OK
```

Oracle had exactly 4 open-stage PRs (ci/ready/parked):
- PR #260 → ci (matched)
- PR #256 → ready (matched)
- PR #258 → ready (matched)
- PR #261 → parked (matched)

The 222 oracle-only PRs are merged/closed/queued PRs Plotroom tracks historically that ccswitch's
`states: OPEN` GraphQL query does not return. This is the same population gap as Phase 0 — expected,
not a failure. Per the brief design, normalize only emits `stage` into OraclePr.fields when the
oracle stage is in the open set, so non-open PRs are silently skipped by the harness (population
differences are reported but do not fail parity).

## Classifier Logic
```
derive_open_stage(checks, merge_state_status, is_draft) -> "parked" | "ci" | "ready"
  is_draft → parked
  checks == "failing" → parked
  checks == "pending" → ci
  merge_state_status == "DIRTY" → parked
  else → ready
```
This is a rollup-based first cut. Plotroom uses per-check required-prefix machinery
(CANCELLED→ci/retrying, ciProgress%, UNKNOWN-holds-prev nuances). On a live population of 4
open PRs, the rollup approximation agreed perfectly. The rollup-vs-required-check gap is the
expected divergence source for 1b; it will quantify with more open PRs.

## Concerns
- The 4 open-PR population is very small (most PRs in this repo are merged/historical).
  Zero mismatches is a clean result, but not yet stress-tested against a larger open-PR
  backlog. The harness is in place and will catch divergence as more open PRs accumulate.
- CANCELLED→ci/retrying, ciProgress% hold-on-UNKNOWN, and autoMergeArmed substate are
  not implemented. These will show as `ready` vs Plotroom's finer substates for some edge
  cases — the harness will surface them once that population is live.
- No push/PR/merge performed; controller integrates per brief constraints.
