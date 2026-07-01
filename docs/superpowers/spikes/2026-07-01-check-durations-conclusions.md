# Spike: check_durations conclusion vocabulary (Insights Task 1)

**Date:** 2026-07-01 · verified against the live `~/.claude-accounts/pipeline_history.db` (read-only).

## Findings

`check_durations`: **55,583 rows** across 5 repos (`cairnea/KinDash`, `steiner385/cc-account-switcher`, `steiner385/plotroom`, `steiner385/pr-dashboard`, `steiner385/reasonbridge`).

Distinct `conclusion` values (all **UPPERCASE**):

| conclusion | count |
|---|---|
| `SUCCESS` | 50,871 |
| `FAILURE` | 2,844 |
| `CANCELLED` | 1,743 |
| `SKIPPED` | 125 |

- `head_sha` populated on 41,457 rows (rows without it bucket under `""` — acceptable).
- `max(run_attempt)` = **14** → retries genuinely occur, so retry-induced flakes are detectable.

## Confirms the plan (no changes)

- `slowest_checks` filters `conclusion='SUCCESS'` — matches the uppercase data (and mirrors the existing `expected()` query).
- Flake normalization lowercases before comparing, so `pass={success}`, `fail={failure, cancelled, timed_out, error, action_required}` covers the observed set (`SUCCESS`→pass; `FAILURE`/`CANCELLED`→fail; `SKIPPED`→ignored). `timed_out`/`error`/`action_required` are unseen here but kept defensively.
