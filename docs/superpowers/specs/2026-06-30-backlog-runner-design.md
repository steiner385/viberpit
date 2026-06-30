# Backlog Runner — Design

**Date:** 2026-06-30
**Status:** Approved (brainstorming) → ready for implementation plan
**Roadmap:** Third of three Automation-tab specs. Builds on
`2026-06-30-automation-tab-shell-design.md` (registry/contract/spawn/per-target-state) and hands off
to `2026-06-30-pr-doctor-design.md` (PR Doctor keeps the runner's PRs green). Consumes both; extends
neither.

## Problem

ccswitch can run and supervise sessions, but a human still has to decide *what* to work on, open a
session, and shepherd each issue to a merged PR. The existing `prioritize-issues` / `auto-prioritize`
/ `implement-loop` skills are manual Claude Code workflows — there is no autonomous, server-side loop
that continuously pulls the highest-value work off the backlog and drives it to a reviewed-ready PR.

The Backlog Runner is that loop: it selects eligible issues (label gate + LLM ranking), claims them,
spawns implementation sessions up to a concurrency cap, and frees a slot once each PR is open with
auto-merge armed — at which point PR Doctor owns keeping it green until it lands. It closes the
automation pipeline: **backlog → session → PR → PR Doctor → merge.**

## Goals

- A supervised control loop that keeps up to **N** implementation sessions in flight, refilling a
  slot as each frees, drawing from a label-gated, LLM-ranked candidate set.
- **Batch-aware:** a work item is 1..K issues → 1 session → 1 PR, where K (`max_issues_per_pr`,
  default 1) is configurable and batched issues are a *coherent* group, not arbitrary top-K.
- **Hand off to PR Doctor:** free a slot when the session's PR is open with auto-merge armed (if PR
  Doctor covers the repo); else hold until merge and recommend enabling PR Doctor.
- **Stay in control:** claim issues so neither a human nor the runner double-picks; bound per-issue
  retries with give-up/unclaim/needs-human; honor the existing account usage-ceiling/all-limited gate.
- Ride the shell entirely — `spawn_session` (provenance + de-dup), per-target state, activity feed,
  config row. **No new persisted tables.** (One small `PullRequestInfo` field is added, shared with
  PR Doctor's detection extensions.)

## Non-goals

- No new automation framework (the shell) and no PR-recovery logic (PR Doctor's job).
- No issue triage/labeling beyond claim/unclaim/needs-human (the runner consumes labels; humans/other
  tools curate the backlog and the eligible label).
- No daily/budget rate cap in v1 — runaway is bounded by concurrency `N` + per-issue attempt limit +
  issue-claiming + the inherited all-limited account gate (a budget cap can be added later).
- No cross-repo issue batching (a batch is within one repo / one PR).
- No new session-spawning mechanism (the shell owns `spawn_session`).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Issue selection | **Hybrid**: label-gated candidate query → LLM tiebreak ranking |
| LLM ranking cost | A cheap **one-shot model query** (reuse the prioritize-issues prompt), not a full session |
| Concurrency | **Configurable N** (`max_concurrent`, default 2); top up as slots free |
| Slot-free / done | PR open **with auto-merge armed** (if PR Doctor covers the repo) → free; else hold until merge |
| Batching | `max_issues_per_pr` (default 1); when >1 the ranking returns a **coherent** batch |
| Guardrails (v1) | **Claim the issue** + **per-issue attempt limit** (no daily budget cap) |
| Account safety | Inherited: reuse the orchestrator's account selection + all-limited gate |
| Detection add | `auto_merge_enabled` on `PullRequestInfo` (GraphQL `autoMergeRequest`) |

## Architecture

### 1. The control loop — `server/automations/backlog_runner.py` (+ pure `backlog_selection.py`)

The registered automation `backlog-runner` (id/name/modes/config/status, from the shell pattern) plus
an orchestrator-owned async loop (a sibling of the pollers). The loop is **idempotent and
restart-recoverable**: its in-flight set is derived from the DB (`Session.origin == "backlog-runner"`
+ non-terminal), never from process memory, so a restart resumes without double-spawning.

Loop tick (on an interval, e.g. 60s, and nudged when a runner session ends):
1. If `mode == "off"` → idle.
2. `in_flight = count(sessions where origin == "backlog-runner" and status != "ended")`.
3. `free = max_concurrent - in_flight`; if `free <= 0` → done this tick.
4. If no healthy (non-limited) account is available → emit `skipped_no_account` (once, debounced),
   done this tick.
5. For each free slot (until candidates run out): run **selection** (§2), **claim** (§4), and either
   spawn (`auto`) or surface a propose action (`propose`).

Pure, unit-tested pieces live in `backlog_selection.py`:
`rank_and_batch(candidates, max_issues_per_pr, ranking) -> Batch` and
`should_free_slot(pr_info, pr_doctor_active) -> bool`.

### 2. Selection pipeline (per refill)

1. **Candidate query (label-gated, `gh` GraphQL):** open issues in `enabled_repos`, **not**
   assigned, **not** already claimed (no `in-progress` runner label / not an active `origin_ref`),
   not blocked, carrying `eligible_label` (config, default `ready`). Reuse `gitrepos`/`gh api graphql`
   (constitution: GraphQL for reads).
2. **Rank + batch (LLM tiebreak):** if ≤1 candidate, take it (no LLM). Else a **one-shot model query**
   (reusing the `prioritize-issues` ranking prompt, run like the existing one-shot Haiku
   title-generator — `generate_title` pattern — *not* a spawned session) returns a ranked order. When
   `max_issues_per_pr == 1`, take the top issue. When `> 1`, the prompt is asked to return a
   **coherent group** (the top issue + issues genuinely related to it: same area/files/feature) up to
   the cap. The pure `rank_and_batch` assembles the `Batch` from the model's answer + the cap.
3. Hand the `Batch` (1..K issue refs, one primary) to claim + spawn.

`Batch = {primary: "owner/name#1", issues: ["owner/name#1", "owner/name#7"], repo: "owner/name"}`.

### 3. Spawn + handoff

- **Spawn:** `ctx.spawn_session(origin_ref=batch.primary, dedupe_key=batch.primary, repo=batch.repo,
  account=<healthy account>, name=…, …)`. The first prompt instructs the session to implement **all**
  issues in the batch and follow the repo's completion workflow, closing every issue with `Fixes #a`,
  `Fixes #b`, … in the PR body (CLAUDE.md auto-close rule), producing **one** PR. The full batch is
  recorded in the spawn event's `detail: {issues: [...]}`.
- **Handoff / free slot:** the loop watches each runner session's PR (via the PR poller/status for the
  session's branch). `should_free_slot(pr_info, pr_doctor_active)`:
  - if PR Doctor is enabled **and** the repo is in PR Doctor's scope → free when the PR is **open and
    `auto_merge_enabled`** (reviewed-ready, auto-merge armed); emit `handoff`.
  - else → free only when the PR is **merged**; emit a one-time `pr_doctor_recommended` notice.
- Freed slots are refilled on the next tick.

### 4. Claim, de-dup, give-up

- **Claim (before spawn):** for **every** issue in the batch, add the `in-progress` runner label (and
  a brief comment linking the session), via `gh`. Combined with `dedupe_key=batch.primary` (shell
  de-dup blocks a second session for the same primary) this prevents double-pick by humans or the
  runner. Claim is best-effort; a claim failure aborts that pick (emit `warn`) rather than spawning an
  unclaimed session.
- **Per-issue attempt limit:** per-target state keyed by `batch.primary`
  `{attempts:int, gave_up:bool, issues:[...]}`. A runner session that ends **without** a mergeable PR
  increments `attempts`. At `max_attempts` → emit `gave_up`, **unclaim all** batch issues (remove the
  runner label / assignment), label each `needs-human`, and never re-pick them (the `needs-human`
  label excludes them from the candidate query). Below threshold, the issue returns to the candidate
  pool (unclaimed) for a later retry.

### 5. Modes + config schema

Master `mode ∈ {off, propose, auto}`: `off` = idle; `propose` = run selection + claim, then surface
the picked `Batch` as a one-tap "Implement #N" approval (spawn only on tap); `auto` = spawn
autonomously. Config (shell-rendered):

| key | type | default | meaning |
|---|---|---|---|
| `enabled_repos` | repo-multiselect | `[]` (all) | repos to draw issues from |
| `eligible_label` | string | `ready` | label gate for candidate issues |
| `max_concurrent` | int | `2` | implementation sessions in flight |
| `max_attempts` | int | `2` | per-issue give-up threshold |
| `max_issues_per_pr` | int | `1` | batch size (1 = one issue per PR) |

### 6. Status & activity

- `status(ctx)` (cheap; from the in-flight count + candidate count + per-target state the loop
  maintains, never a live `gh` call on render): e.g. `"2 implementing, 5 eligible, 1 needs-human"`,
  level `active` (work in flight) / `warn` (something needs human) / `idle`.
- Events (`ctx.emit(kind=…)`, issue/PR-tagged, session-linked): `selected` (with `detail.issues`),
  `claimed`, `spawned`, `pr_opened`, `handoff`, `pr_doctor_recommended`, `gave_up`, `unclaimed`,
  `skipped_no_account`.

## Data flow

```
loop tick (60s / on runner-session end):
  mode off? → idle
  in_flight = sessions(origin="backlog-runner", status!="ended")
  free = max_concurrent - in_flight; if free<=0 → done
  no healthy account? → emit skipped_no_account → done
  repeat up to `free` times while candidates remain:
    candidates = gh GraphQL: open, in enabled_repos, eligible_label, unassigned, unclaimed, not needs-human
    batch      = rank_and_batch(candidates, max_issues_per_pr, one_shot_rank())   (pure + 1 LLM call)
    st         = ctx.get_target_state(batch.primary)
    if st.gave_up → skip
    claim all batch issues (label + comment)               (abort pick on failure)
    mode auto    → ctx.spawn_session(origin_ref=batch.primary, dedupe_key=batch.primary, …); emit spawned
    mode propose → surface "Implement {primary}" action; (spawn on tap)
  for each in-flight runner session:
    pr = pr_status(session.branch)
    if should_free_slot(pr, pr_doctor_active) → emit handoff/merged; slot frees next tick
    if session ended without mergeable PR → attempts++; at max → gave_up + unclaim all + needs-human
```

## Error handling

- **No healthy account:** never spawn; emit `skipped_no_account` (debounced); resume when an account
  frees. Reuses the orchestrator's existing account-availability logic.
- **`gh` candidate query fails:** skip the tick (emit `warn`); the loop retries next interval.
- **Claim fails** (label/comment): abort that pick without spawning (emit `warn`); the issue stays in
  the pool. Never spawn an unclaimed session (would risk double-work).
- **LLM ranking fails / times out:** fall back to a deterministic order (e.g. issue number / oldest
  first) so the loop still progresses; emit `warn`.
- **Spawn fails / de-dup hit:** the shell handles de-dup (returns existing session, no double-spawn);
  a real spawn failure emits `error`, unclaims the batch, and frees the attempt (not consumed).
- **Restart mid-flight:** in-flight is recomputed from the DB; claimed-but-unspawned issues (claim
  succeeded, crash before spawn) are reconciled on the next tick (an in-progress label with no live
  `origin_ref` session → re-pick allowed after a grace, or surfaced as `needs-human`). Keep this
  reconciliation simple and conservative (prefer re-claim over double-spawn; de-dup is the backstop).

## Acceptance criteria

1. The loop keeps ≤ `max_concurrent` runner sessions in flight; refills as slots free; idles at `off`.
2. Selection: label-gated candidates only; `≤1` candidate skips the LLM; `>1` uses the one-shot rank;
   `max_issues_per_pr>1` yields a coherent batch ≤ cap.
3. A spawned session carries `origin="backlog-runner"`, `origin_ref=primary`, batch in `detail`; the
   shell de-dup prevents a second session for the same primary.
4. Slot frees at PR-open-with-auto-merge when PR Doctor is active for the repo; else at merge (+ the
   recommend notice). `should_free_slot` is correct for both branches.
5. Claim labels every batch issue; give-up at `max_attempts` unclaims all + labels `needs-human` +
   never re-picks; below threshold the issue returns to the pool.
6. No healthy account → no spawn + `skipped_no_account`; restart recomputes in-flight from the DB (no
   double-spawn).
7. `auto_merge_enabled` round-trips through `PullRequestInfo.to_dict`/`from_dict`.
8. Server `pytest` green.

## Testing (TDD)

**Pure selection (`server/tests/test_backlog_selection.py`):**
- `rank_and_batch`: 0/1/many candidates; `max_issues_per_pr` 1 vs K; model returns an order →
  expected `Batch`; malformed model output → deterministic fallback.
- `should_free_slot`: (pr open + auto_merge on/off) × (pr_doctor_active true/false) × merged →
  expected boolean.

**Detection (`server/tests/test_pr_status_auto_merge.py`):**
- `build_pr_query` requests `autoMergeRequest`; `parse_pr_response` sets `auto_merge_enabled`;
  round-trip via `to_dict`/`from_dict`. (May share a file with PR Doctor's `mergeable` tests.)

**Loop integration (`server/tests/test_backlog_runner_loop.py`, fakes for gh/LLM/orchestrator):**
- Tops up to `max_concurrent`, no more; refills when a fake session ends with a mergeable PR.
- `off` idles; `propose` claims + surfaces an action but does not spawn; `auto` spawns.
- Claim-all on a batch; give-up unclaims-all + `needs-human` at `max_attempts`; re-pick below.
- No-account → `skipped_no_account`, no spawn; restart recomputes in-flight (no double-spawn).
- De-dup: two ticks for the same primary → one session.

## Out of scope / YAGNI

- Daily/budget rate cap (concurrency + attempt-limit + claim bound runaway for v1).
- Cross-repo batching.
- Issue triage/curation, dependency-graph awareness, or auto-creating the `eligible_label`.
- A full triage *session* for ranking (one-shot query only).
- New persisted tables (rides the shell; one shared `PullRequestInfo` field only).

## Open implementation notes

- Keep `rank_and_batch` + `should_free_slot` pure; the loop is the only I/O boundary.
- Reuse: account selection + all-limited gate from the orchestrator; the one-shot model call follows
  `generate_title` / `_default_title_fn`; the candidate query uses `gitrepos` + `gh api graphql`; the
  implementation prompt leans on the repo's own completion workflow (CLAUDE.md) rather than
  re-encoding it.
- The `auto_merge_enabled` field is added to `PullRequestInfo` once and shared with PR Doctor — if
  both specs are implemented, the field is added by whichever lands first; the other reuses it (no
  parallel field). Coordinate in the plans.
- In-flight derivation from `Session.origin` (not process memory) is the key to restart-safety —
  do not cache the in-flight set anywhere durable.
