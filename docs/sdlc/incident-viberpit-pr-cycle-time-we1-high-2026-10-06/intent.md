---
initiative: incident-viberpit-pr-cycle-time-we1-high-2026-10-06
kind: intent
verified_against: 17204bd8add9651520c49407768d57e6658c525e
---

# Intent — viberpit-pr-cycle-time we1 (high) breach, 2026-10-06

## Problem

**Verdict: a measurement artifact. Throughput did not regress. This is the same
detector defect as the 2026-09-25 incident, and the fix that incident called for
never shipped.** Recommend closing the incident once the detector guard is built.

The breach is a two-PR day bucket. `viberpit-pr-cycle-time` (`server/sdlc/bands.yaml`,
source `pr_cycle_time_hours`) scores the mean open→merge hours of PRs merged on a UTC
day (`bucket_daily`, `server/sdlc/control_bands.py`). The 2026-10-06 bucket held
exactly two PRs when the incident was filed (checked with `gh`):

| PR | opened | merged | cycle |
|----|--------|--------|-------|
| #2705 | 2026-10-06T13:18Z | 2026-10-06T13:30Z | 0.19h |
| #2700 | 2026-10-05T20:40Z | 2026-10-06T14:46Z | 18.10h |

(18.10 + 0.19) / 2 = 9.146h, which is the reported value to the digit. The read ran
at about 14:47Z, so the bucket was a partial UTC day with n=2. The baseline (mean 0.50h,
σ 0.68h over 23 daily means) is built from days that typically hold dozens of PRs at
~10–15 min each. One 18h PR is a +12.68σ point against a bucket that size and says
nothing about the loop.

Why #2700 was slow: it is a real, ordinary straggler, not a loop failure. It opened at
20:40Z, auto-merge was armed at 20:41Z, and it had a single commit until a
`Merge branch 'master'` commit at 14:36Z the next day. It then merged 10 minutes later.
So it waited about 18h with nothing re-pushing it. The cause (red or BEHIND with no
update) was not determined here, and `statusCheckRollup` was not retained. It is the
same shape as #2397 on 09-25 (a PR waiting hours for someone to re-push), so that
secondary question is worth its own signal (see Open questions), but it is not what
tripped the alarm.

Why this is a recurrence: the 09-25 intent
(`docs/sdlc/incident-ccswitch-pr-cycle-time-we1-high-2026-09-25/intent.md`) diagnosed the
identical cause and asked for a guard (drop the in-progress day and/or require a minimum
observations-per-bucket). Its regression case was pulled from master (#2412) to keep the
required eval gate green, with the intent that it return in the fix PR. No fix PR
followed. On `origin/master` (17204bd8) `bucket_daily` is unchanged (last touched
2026-09-07, #1386) and there is no `partial_day` test, `min_bucket_observations` knob,
or as-of boundary anywhere in `server/sdlc/`. Nothing excludes today's bucket or a thin
one, so every straggler that merges early in a UTC day can re-file this incident.

## Desired outcome

- A day bucket that is still in progress (its UTC date is today) or thinly populated can
  never be a sample that a rule scores or that feeds `compute_baseline`. A lone slow PR
  cannot fire we1/we2/we3.
- A genuine full-day regression still breaches. The next complete UTC day after a real
  stall is scored normally.
- The guard ships with the regression eval that was deferred in #2412 (see Open
  questions) and unit tests in `server/tests/test_sdlc_control_bands.py`.
- Verified by replay: the 2026-10-06 and 2026-09-25 buckets, replayed through the
  guarded series reader as of the incident filing time, produce no breach.
- Incident closed with root cause "partial-day bucket, n=2". Close the 09-25 incident
  the same way if it is still open.

## Affected systems

- `server/sdlc/control_bands.py`: `bucket_daily` / `Sample` (carry an observation count,
  or take an as-of boundary).
- `server/sessions/orchestrator.py`: `_control_band_read_series` and
  `_control_band_pr_cycle_observations`, the seam that knows "now".
- `server/sdlc/bands.yaml` and `server/sdlc/bands.py` if the guard is a per-band knob
  (agent configuration, so it triggers the Evals workflow).
- `server/tests/test_sdlc_control_bands.py`, `server/tests/test_control_band_monitor.py`.
- `ci_failure_rate` shares the day-bucket shape and the same hazard (a sibling per
  `capturing-fix-patterns`).

## Constraints

- Constitution VI: fix the one bucketing path. No parallel "safe" reducer next to
  `bucket_daily`. Any new setting takes the `VIBERPIT_` prefix.
- Detection stays pure and deterministic (no model). The clock is injected, never read
  inside `control_bands.py`.
- Keep `we1..we4` semantics and `EVAL_POINTS` unchanged. Only sample eligibility changes.
- Do not weaken the 3σ tier or widen σ to make this go away.
- Scope: no change to CI, auto-merge, or PR Doctor here.
- DB clock rule: naive UTC only, no `func.now()`.
- New docs and eval descriptions must not spell the legacy brand (the brand ratchet's
  `persisted-keys` entry is at its ceiling).

## Open questions

1. **No regression eval is committed with this intent, on purpose.** The only honest
   check is behavioural: it must exit non-zero while the guard is absent. It cannot be
   made to pass today, because the guard does not exist. The required test
   `server/tests/test_sdlc_evals_runner.py::test_static_gate_passes_on_current_repo`
   runs every case under `evals/regressions/` against the current tree, so merging a
   case that fails now turns master red. That already happened once (#2412). A check
   that passes today would be vacuous, e.g. a grep for this incident's id or a
   `|| true`. So the case is deferred to the fix PR. Add it in the same PR as the guard
   as `evals/regressions/incident-pr-cycle-time-we1-partial-day-bucket.yaml`, with
   `kind: static`, `tags: [sdlc, control-bands, regression]`, and
   `check: python -m pytest -q server/tests/test_sdlc_control_bands.py -k partial_day`.
   Pytest exits 5 if no such test exists and non-zero if one fails, so it fails today
   and after any revert of the guard. Settle the `-k partial_day` naming convention in
   the spec.
2. **Guard shape**: drop the in-progress UTC day, require N observations per bucket, or
   both? Recommend both: drop today, and add a per-band `min_bucket_observations`. Pick
   the default from historical daily counts. Note that this day's n=2 would pass an N=3
   floor only if the day were complete, so the in-progress drop is the load-bearing half.
3. Should the guard be a hard filter or a "deferred, re-evaluate when the bucket
   closes" state? A deferral keeps a real stall from being masked for a day, at the cost
   of a delay of up to 24h before a real slowdown can file.
4. A PR that sits 18h (#2700) or ~3h (#2397) with auto-merge armed and no re-push is a
   real, repeating pattern. Is it worth a separate signal (e.g. PR age with armed
   auto-merge, or a BEHIND-too-long alert)? Not part of this incident. First find out
   what #2700 was waiting on.
5. Should `ci_failure_rate` get the same guard now, or after an audit?
6. Why did the 09-25 follow-up never get picked up? If the cause is that the incident
   was auto-closed or went stale, the incident workflow may need to hold the intent open
   until a fix PR references it.
