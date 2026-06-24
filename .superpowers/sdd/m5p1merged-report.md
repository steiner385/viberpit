# M5 Phase 1 Merged-Stage Report

## Status
COMPLETE — all gates green; live parity matched=229 mismatches=0.

## Commit SHAs
- `51c1c1f` — feat(parity): add derive_merged_stage classifier (Phase 1 merged slice)
- `6c9c571` — feat(parity): normalize emits stage=merged + drops checks for merged PRs
- `37385d8` — feat(parity): candidate fetches merged PRs within retention window

## Test Summary
43 passed in 0.12s (server/tests/test_pipeline_stage.py, test_parity_normalize.py,
test_parity_cli.py, test_parity_harness.py, test_parity_coverage.py). ruff: all
checks passed. mypy: no issues (7 source files).

## Live Parity Result
```
matched: 229
mismatches: 0
oracle-only: 0  candidate-only: 0
PARITY OK
```
Phase 1 (`--phase 1`) with `--repos steiner385/cc-account-switcher`.
Previously open-only (4 matched); now 229 matched covering open + merged PRs.

## Files Changed
- `server/pipeline/stage.py` — `derive_merged_stage` added; parses ISO-8601 (incl. `Z`),
  age > retention_days → None, has_deploy True → None (TODO slice 1d).
- `server/tests/test_pipeline_stage.py` — 6 new merged-stage unit tests.
- `server/github/parity/normalize.py` — `_MERGED_STAGES` frozenset; oracle emits
  `stage="merged"` for merged PRs; drops `checks`/`checks_detail` for merged PRs
  (frozen rollup avoidance).
- `server/tests/test_parity_normalize.py` — 3 new tests (merged stage emitted,
  open has checks, merged drops checks); updated existing open-stage test comment.
- `server/github/parity/__main__.py` — `_merged_pr_list_args` helper; second fetch
  in `compute_candidate_by_number` via `gh pr list --state merged`; `derive_merged_stage`
  imported and used; off-board PRs skipped.
- `server/tests/test_parity_cli.py` — 3 new CLI tests (recently-merged included,
  off-board excluded, existing tests updated to inject empty merged list).

## Concerns
None. The retention window (7 days) means off-board merged PRs appear oracle-only
(Plotroom still shows them); this is by design (the harness reports them informational,
not a parity failure). The `has_deploy=True` path returns None (placeholder for
slice 1d deploy stages) — tested and documented as TODO.
