# Intent — ccswitch-pr-cycle-time we1 (high) breach, 2026-09-25

## Problem

**Verdict: mostly a measurement artifact. The delivery loop did not regress.** The
one real thing underneath is two PRs blocked by CI failures that were not their own.
The detector has a defect that turned that into a 3σ incident.

What the breach is: `ccswitch-pr-cycle-time` (`server/sdlc/bands.yaml`, source
`pr_cycle_time_hours`) is the mean open→merge hours of PRs merged on a **UTC day**
(`bucket_daily`, fed by `_control_band_pr_cycle_observations` in
`server/sessions/orchestrator.py`). The newest sample, `2026-09-25` = 3.462h, is
**one PR**: #2397 opened 2026-09-24T20:51:07Z, merged 2026-09-25T00:18:50Z
(3h27m43s = 3.462h). The incident was filed at ~00:22Z, four minutes into the UTC
day, when that bucket held a single observation. (#2401, opened 21:16Z, merged
00:21:38Z at 3.08h, landed after the read.)

Why 21.6σ: the baseline is a mean of *daily means* over dense days (dozens of PRs,
~10–15 min each), so σ is 0.143h. A day bucket holding one slow PR is a different
population from a day averaging ~40 PRs, but the detector compares them as
equals. Nothing in `control_bands.py` or `control_band_monitor.py` excludes the
in-progress day or requires a minimum observations-per-bucket.

Why those two PRs were slow (checked with `gh run view`):
- **#2397**: first CI run (20:51Z) failed `lint-and-test-core` on mypy errors in
  `server/pm/triage_accept.py`, `server/sessions/repo_match.py`, `orchestrator.py`,
  i.e. the SQLAlchemy 2.1.0 typing break that was red on master. The pin fix (#2399)
  merged at 21:05Z, but #2397 was not rebased until 00:09Z, then went green and merged.
- **#2401**: first CI run (21:16Z) failed one `server-tests (1)` test with
  `OSError: [Errno 39] Directory not empty` in `test_accounts_extra_usage.py`, the
  known temp-dir teardown flake. It was re-pushed at 00:11Z and merged.
- The rest of the 09-24 window merged at the usual ~10–15 min (`gh pr list`, 60 PRs).

So the incident is a true but small signal ("two PRs waited ~3h on red CI before
anyone re-pushed") reported through a detector that scores an unfinished, n=1 day as a
full-day mean. The transient causes are already fixed (#2399) or known
(temp-env Errno 39).

Recommendation: **do not chase PR throughput.** Fix the detector so this class of
false positive cannot recur, then close the incident.

## Desired outcome

- A day bucket that is still in progress (its UTC date is today) or is thinly
  populated cannot be one of the samples a rule scores. The series reader either
  drops it or requires a minimum observation count per bucket, so a lone PR cannot
  fire we1/we2/we3.
- The baseline (`compute_baseline`) is not fed partial buckets either.
- A genuine full-day regression still breaches: the guard must not mask a real
  slowdown. The next full UTC day after a real stall is scored normally.
- Verified by the regression eval `evals/regressions/incident-pr-cycle-time-we1-partial-day-bucket.yaml`
  and by unit tests in `server/tests/test_sdlc_control_bands.py`.
- Incident closed with the root cause recorded as "partial-day bucket + n=1".

## Affected systems

- `server/sdlc/control_bands.py`: `bucket_daily` / `Sample` (carry an observation
  count, or accept an as-of boundary).
- `server/sessions/orchestrator.py`: `_control_band_read_series` and
  `_control_band_pr_cycle_observations` (the seam that knows "now").
- `server/sdlc/bands.yaml` and `server/sdlc/bands.py` if the guard is a per-band
  knob (e.g. `min_bucket_observations`). `bands.yaml` is agent configuration and
  triggers the Evals workflow.
- `server/tests/test_sdlc_control_bands.py`, `server/tests/test_control_band_monitor.py`.
- `ci_failure_rate` series shares the same day-bucket shape; check it for the same
  hazard (see `capturing-fix-patterns`).

## Constraints

- Constitution VI: fix the one bucketing path; no parallel "safe" reducer next to
  `bucket_daily`. Any new setting takes the `VIBERPIT_` prefix (brand ratchet).
- The detector stays pure and deterministic (no model in detection); the clock is
  injected, not read inside `control_bands.py`.
- Keep `we1..we4` semantics and `EVAL_POINTS` unchanged; only which samples are eligible changes.
- Do not weaken the 3σ tier or widen σ to make this incident go away.
- Scope: no change to CI, the pin, or PR Doctor in this work; the ~3h stall on
  red CI is noted, not fixed here.
- DB clock rule: naive UTC only, no `func.now()`.

## Open questions

1. **Guard shape**: drop the in-progress UTC day, require N observations per
   bucket, or both? Recommend both: drop today, and add a per-band
   `min_bucket_observations` (default 3?). The spec should pick the default from the
   historical daily counts.
2. **Regression eval is expected to fail until the fix lands.** No behavior-level
   check is possible today because the guard's API does not exist, so the case
   asserts a named test (`-k partial_day`) exists and passes in
   `test_sdlc_control_bands.py`; it exits 5 on master today. The Evals workflow is
   not a required check, but it runs on `evals/**` pushes to master and will be red
   until the fix PR merges. Land the fix promptly, or ask whether to add the case in
   the fix PR instead. The `-k partial_day` naming convention is the coupling to
   settle in the spec.
   **Resolved (2026-09-24): the case lands with the fix PR.** The static gate is also
   enforced by the required `server/tests/test_sdlc_evals_runner.py`, so shipping the
   case ahead of the guard turned master red; it was removed and must be re-added,
   `check: python -m pytest -q server/tests/test_sdlc_control_bands.py -k partial_day`,
   in the same PR as the guard. Its description must not spell the band id (the brand
   ratchet's `persisted-keys` entry sits at its ceiling).
3. Is the ~3h wait of a red-CI PR with no re-push (#2397) worth its own signal? Not
   part of this incident, but PR Doctor / CI-repair dispatch might warrant a look if
   it recurs.
4. Should `ci_failure_rate` get the same guard now, or only after an audit?
