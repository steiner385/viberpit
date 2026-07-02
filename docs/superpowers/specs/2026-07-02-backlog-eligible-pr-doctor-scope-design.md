# Backlog eligibility + PR Doctor scope + archived-slot recycling — Design

**Date:** 2026-07-02
**Status:** Approved. **Fix C (archived-slot recycling) DROPPED during implementation** —
superseded by PR #421 ("auto-archive a spawned session when it signals completion"), which
merged after this design was written. #421 already auto-archives completed backlog sessions and
makes `_backlog_session_ended` treat `archived` as terminal so the sweep reclaims the slot —
delivering the archived-capacity behavior this design's Fix C aimed for (and #421 deliberately
charges an attempt / escalates to needs-human for a completed run with no PR, which the original
Fix C would have contradicted). Only **Fix A** (PR Doctor repo-wide scope) and **Fix B** (true
eligible count) were implemented. Fix A/B sections below stand; Fix C is retained only as a record.

## Problem

Three related dashboard/automation defects, discovered from a live session where the
Automation panel showed "Backlog Runner: 4 implementing, 0 eligible" and "PR Doctor: all PRs
green" while the pipeline dashboard showed multiple genuinely-failing PRs.

1. **PR Doctor is blind to PRs it didn't author.** Both the auto-fix sweep
   (`orchestrator.py:_pr_doctor_sweep`, ~line 1298) and the count
   (`pr_doctor_actionable`, ~line 2198) iterate `_pr_pairs()` — only `(repo, branch)` pairs a
   ccswitch session authored (live `_records` + DB `authored_branches`/`branch`). Red PRs that
   no ccswitch session created (e.g. `steiner385/ccswitch#383`, `#395`, `cairnea/KinDash#10886`)
   are never classified, so `failing` stays 0 and the status reads "all PRs green".

2. **Backlog "eligible" is a refill-loop byproduct, not a backlog gauge.**
   `status()` renders `counts["eligible"]` = `self._last_eligible` (`backlog_runner.py:398`),
   which is only written *inside* the refill loop: `len(batch.issues)` of the selected batch
   (`:460`, a running `max`) or reset to 0 (`:458`). The loop body runs
   `for _ in range(max(0, max_concurrent - in_flight))` (`:440-441`), so when the runner is
   saturated it never queries candidates and `eligible` is stuck at 0 — regardless of how many
   ready issues actually exist. Even when it does run, `len(batch.issues)` is bounded by
   `max_issues_per_pr` (default 1), never the true candidate count.

3. **Archived sessions keep holding runner slots (root cause of #2's symptom).**
   `_backlog_count_in_flight` (`orchestrator.py:902`) counts active (non-`gave_up`) durable
   target states. A slot is freed only by the sweep on: PR merged / handed to PR Doctor /
   gave-up / max-attempts exhausted (`backlog_runner.py:_sweep_one`). A spawned session that has
   been **archived** but whose target hasn't hit one of those conditions keeps occupying a slot.
   The observed "4 implementing" were archived sessions, holding the cap at capacity and
   suppressing new spawns.

## Decisions (from brainstorming)

- **PR Doctor scope:** scan *all* open PRs in configured repos and act on any red PR (not just
  authored). Accepts that `auto` mode will now spawn recovery sessions for human-authored PRs.
- **UNSTABLE CI:** leave `classify()` as-is — a `failing` check with `mergeStateStatus == UNSTABLE`
  (non-required check red) stays non-actionable. No change.
- **Backlog eligible:** report the true candidate count every tick, independent of free slots.
- **Archived capacity:** archived/terminal sessions must not count toward saturation. Route this
  through the existing sweep machinery, not a parallel count-side filter.

## Fix A — PR Doctor: scan all open PRs in configured repos

**Source of truth already exists:** `self._pr_population` (`PrPopulationPoller`) polls every open
PR per watched repo (`.all() -> {repo: (PullRequestInfo, ...)}`, repos from `_pr_repos`). It
already backs the pipeline view (`orchestrator.py:2096`). Do NOT build a new scanner
(constitution VI / eliminating-parallel-implementations).

**Change:**
- Add `_pr_doctor_scope_pairs()` returning the union of:
  - `(repo, branch)` for every PR in `self._pr_population.all()` whose repo is in PR Doctor's
    configured scope (empty scope = all watched repos, matching `_backlog_pr_doctor_active`), and
  - `_pr_pairs()` (preserves authored/archived-session coverage even if population hasn't polled).
- Point both `_pr_doctor_sweep` (~1298) and `pr_doctor_actionable` (~2198) at the new pair set.
- Resolve `PullRequestInfo` for each pair preferring the population cache, falling back to
  `pr_for()` (the per-branch `PrStatusPoller` cache), so no double-poll is required.
- `classify()` unchanged.

**Effect:** repo-wide red PRs are counted and (in `auto`) acted on, subject to the existing
per-head-SHA dedupe, terminal-state no-op, and propose/auto gates. "all PRs green" becomes
truthful. Status text "watching N failing PR(s)" now reflects the whole configured scope.

## Fix B — Backlog "eligible" = true candidate count

**Change:** in `_stash_counts` (`backlog_runner.py:382`), compute the eligible count from the
injected candidate query rather than `self._last_eligible`. Introduce a `count_candidates` seam
(wired in the orchestrator to `len(query_candidates(config))`) so the loop stays fully testable
with no network. Report that value as `counts["eligible"]`. `_last_eligible` (batch-size
high-water) is removed to avoid a second, misleading source of the same concept.

- One extra GraphQL search per backlog tick (coarse interval; negligible cost).
- Best-effort: a query failure falls back to 0 (or last-known) and never breaks the tick.
- Semantics: `eligible` = open, unassigned, `ready`-labelled, not-`in-progress`/`needs-human`
  issues in configured repos — the exact `query_candidates` gate.

## Fix C — Archived/terminal sessions stop holding a slot

**Change:** make an archived spawned session count as terminal in the sweep, reusing existing
branches rather than filtering the count:
- Extend the session-liveness probe so a session whose status is `archived` (in addition to
  `ended`) reads as not-alive (`_session_alive` / the `session_ended` seam).
- In `_sweep_one`, when the spawned session is archived/terminal:
  - if an open PR exists for the target → hand off to PR Doctor and free the slot (PR Doctor now
    covers it repo-wide via Fix A);
  - if no PR exists → apply the existing attempt/give-up path immediately.

This keeps the target-state ledger the single authoritative slot accounting; freeing flows
through the same `delete_target_state` calls the sweep already uses.

**Effect:** in-flight drops below `max_concurrent` as sessions are archived → the runner queries
and spawns again → Fix B surfaces the real backlog.

## Interaction / net effect

Fix C frees the stuck slots → runner is no longer saturated → it queries candidates → Fix B
reports the real "eligible" number and the runner resumes spawning. Fix A independently makes PR
Doctor recognize and (in auto) repair the actually-red PRs across configured repos.

## Testing

- **Fix A:** unit-test `_pr_doctor_scope_pairs` union + scope filtering; test that
  `pr_doctor_actionable` counts a population-only red PR (no authoring session). Assert
  UNSTABLE-failing still excluded.
- **Fix B:** loop test with a stubbed `count_candidates` seam asserting `eligible` reflects the
  candidate count while saturated (free slots = 0) and while idle.
- **Fix C:** sweep test — an archived session with (a) an open PR hands off + frees the slot and
  (b) no PR charges an attempt/gives up; assert `_backlog_count_in_flight` drops accordingly.
- Full gates: `ruff`, `mypy`, `pytest server/tests/`; web `tsc`/`eslint`/`vitest` only if any
  status-string/label rendering changes on the frontend.

## Out of scope

- Changing `classify()` UNSTABLE handling.
- Auto-archiving completed backlog sessions (separate TODO; Fix C only stops archived sessions
  from holding slots, it does not archive them).
- Throttling/caching the candidate query (chosen: query every tick).
