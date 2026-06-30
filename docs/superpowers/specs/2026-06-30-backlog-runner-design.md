# Backlog Runner — Design

**Date:** 2026-06-30
**Status:** Revised per CI/CD + PM review (round 2) → ready for implementation plan
**Roadmap:** Third of three Automation-tab specs. Builds on
`2026-06-30-automation-tab-shell-design.md` (registry/contract/spawn/per-target-state), the shared
**PR-status field extension** (adds `auto_merge_enabled`), and hands off to
`2026-06-30-pr-doctor-design.md` (PR Doctor keeps the runner's PRs green). Consumes all three;
extends none.

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
  default 1) is configurable and batched issues are a *coherent* group, not arbitrary top-K. (Kept in
  v1 by explicit product decision — the operator can choose to work several related issues together.)
- **Hand off to PR Doctor — and watch the handoff:** free a slot when the session's PR is open with
  auto-merge armed (if PR Doctor covers the repo), but keep a lightweight **terminal-watch** so a PR
  that never lands (PR Doctor gave up / closed-unmerged) escalates the issue to `needs-human` instead
  of leaking.
- **Stay in control:** claim issues (atomically, self-healing) so neither a human nor the runner
  double-picks; bound per-issue retries with give-up/unclaim/needs-human; a **circuit-breaker** caps
  spawns during the break-in period; honor the existing account usage-ceiling/all-limited gate.
- Ride the shell entirely — `spawn_session` (provenance + de-dup), per-target state, activity feed,
  config row. **No new persisted tables** (one shared `PullRequestInfo` field via the precursor plan).

## Non-goals

- No new automation framework (the shell) and no PR-recovery logic (PR Doctor's job).
- No issue triage/labeling beyond claim/unclaim/needs-human.
- **No full budget/accounting system in v1** — a cheap circuit-breaker (below) is the break-in
  backstop; a richer per-account budget can come later.
- No cross-repo issue batching (a batch is within one repo / one PR).
- No new session-spawning mechanism (the shell owns `spawn_session`).
- No triage *session* for ranking — a one-shot model query only.

## Decisions (from brainstorming + spec review)

| Question | Decision |
|---|---|
| Issue selection | **Hybrid**: label-gated candidate query → one-shot LLM rank |
| Concurrency | `max_concurrent`, **default 1** (review: safer break-in; raise as trust grows) |
| Circuit-breaker | `max_spawns_per_day` (default 10) — pause spawning past the cap; resets daily |
| Slot-free / done | PR open **with auto-merge armed** (if PR Doctor covers the repo) → free; else hold until merge |
| Handoff watch | Keep a terminal-watch on handed-off targets; PR-Doctor `gave_up`/closed-unmerged → `needs-human` |
| Batching | `max_issues_per_pr` (default 1); when >1 the ranking returns a **coherent** batch (kept in v1) |
| Guardrails (v1) | Claim the issue (atomic/self-healing) + per-issue attempt limit + circuit-breaker |
| Account safety | Inherited: reuse the orchestrator's account selection + all-limited gate |
| Restart safety | In-flight derived from `Session.origin` (never process memory); **non-reentrant tick** |
| Detection add | `auto_merge_enabled` via the shared PR-status field extension (lands first) |

## Architecture

### 1. The control loop — `server/automations/backlog_runner.py` + pure `backlog_selection.py`

The registered automation `backlog-runner` (id/name/modes/config/status) plus an orchestrator-owned
async loop (a sibling of the pollers). The loop is **idempotent and restart-recoverable**: its
in-flight set is derived from the DB (`Session.origin == "backlog-runner"` + non-terminal), never from
process memory.

**Non-reentrant by construction (review F4):** the whole refill body runs under a single
`asyncio.Lock` so an interval tick and an end-of-session *nudge* cannot overlap — two concurrent ticks
counting `free` from a DB that doesn't yet show an in-flight spawn would double-spawn into one slot
(`dedupe_key` only guards the *same* primary, not two different primaries). Belt-and-suspenders:
`create_session` persists `Session.origin`/`origin_ref` **first** (reserving the slot in the same
transaction as the dedup check) before the heavy worktree/driver work, so the in-flight count sees a
spawn immediately.

Loop tick (interval ~60s, plus nudged when a runner session ends):
1. `mode == "off"` → idle.
2. If today's spawn count ≥ `max_spawns_per_day` → emit `circuit_open` (once), skip spawning (still
   run the handoff/terminal-watch sweep, step 6).
3. `in_flight = count(sessions where origin == "backlog-runner" and status != "ended")`;
   `free = max_concurrent - in_flight`; if `free <= 0` → go to step 6.
4. No healthy (non-limited) account → emit `skipped_no_account` (debounced) → step 6.
5. For each free slot while candidates remain (and under the daily cap): **select** (§2),
   **claim** (§4), then `auto` → spawn; `propose` → surface an "Implement" action.
6. **Handoff + terminal-watch sweep** over in-flight + handed-off targets (§3).

Pure, unit-tested pieces in `backlog_selection.py`:
`rank_and_batch(candidates, max_issues_per_pr, ranking) -> Batch` and
`should_free_slot(pr_info, pr_doctor_active) -> bool`.

**Split for delivery (two plans):** *Plan A* = control loop + restart-recovery + non-reentrancy +
`should_free_slot`/handoff + terminal-watch (pure pieces in `backlog_selection.py`); *Plan B* =
candidate query + one-shot LLM rank + claim/unclaim/give-up + batching. Plan B's **Task 1 is a
fake-`gh`/fake-LLM/fake-spawn harness** — the full loop suite runs with no network and no real
sessions, and `mode` defaults to `off` so local dev never autonomously spends.

### 2. Selection pipeline (per refill)

1. **Candidate query (label-gated, `gh` GraphQL):** open issues in `enabled_repos`, **not** assigned,
   **not** claimed (no `in-progress` runner label / not an active `origin_ref`), **not** `needs-human`,
   not blocked, carrying `eligible_label` (config, default `ready`). Reuse `gitrepos` + `gh api
   graphql` (GraphQL for reads).
2. **Rank + batch (one-shot LLM):** ≤1 candidate → take it (no LLM). Else a **one-shot model query**
   (reusing the `prioritize-issues` ranking prompt, run like `generate_title` — *not* a spawned
   session, injectable behind a `ranking` seam so tests are deterministic) returns a ranked order.
   `max_issues_per_pr == 1` → take the top. `> 1` → the prompt returns a **coherent group** (top issue
   + genuinely related issues: same area/files/feature) up to the cap. The pure `rank_and_batch`
   assembles the `Batch`; **malformed model output → deterministic fallback** (oldest-issue-first).
3. Hand the `Batch` to claim + spawn.

`Batch = {primary: "owner/name#1", issues: ["owner/name#1", "owner/name#7"], repo: "owner/name"}`.

### 3. Spawn + handoff + terminal-watch

- **Spawn:** `ctx.spawn_session(origin_ref=batch.primary, dedupe_key=batch.primary, repo=batch.repo,
  account=<healthy account>, …)`. First prompt: implement **all** batch issues, follow the repo's
  completion workflow, close each with `Fixes #a`, `Fixes #b`, … (one PR). The batch is recorded in the
  spawn event `detail: {issues: [...]}`. Each spawn increments today's circuit-breaker count.
- **Free a slot** via `should_free_slot(pr_info, pr_doctor_active)`: PR Doctor active for the repo →
  free when the PR is **open and `auto_merge_enabled`** (emit `handoff`); else free only when
  **merged** (emit `merged`, plus a one-time `pr_doctor_recommended` notice).
- **Terminal-watch (review F1 — the livelock fix):** freeing a *slot* does **not** stop watching the
  *target*. The sweep (loop step 6) re-checks handed-off targets via per-target state + `pr_info`:
  - PR merged → emit `merged`, delete target state (issues auto-closed by `Fixes`).
  - PR Doctor emitted `gave_up` for that PR (queryable: `automation_event` where
    `automation_id='pr-doctor'`, `kind='gave_up'`, `detail.origin_ref == batch.primary`) **or** PR
    closed-unmerged → emit `gave_up`, **unclaim all** batch issues, label each `needs-human`, delete
    target state. So an un-landable PR can never leave an issue claimed forever.

### 4. Claim, de-dup, give-up (atomic / self-healing)

- **Claim (before spawn):** record the batch's full issue list in target state **first**, then add the
  `in-progress` runner label + a brief session-linking comment to **every** issue via `gh`. Combined
  with `dedupe_key=batch.primary` (shell de-dup blocks a second session for the same primary) this
  prevents double-pick. **Self-healing rollback:** if `gh` fails mid-batch, best-effort remove the
  labels already added in this batch and abort the pick (emit `warn`) — never spawn a partially-claimed
  or unclaimed batch.
- **Per-issue attempt limit:** per-target state keyed by `batch.primary`
  `{attempts, gave_up, issues:[...], spawn_count}`. A runner session that ends **without** a mergeable
  PR increments `attempts`. At `max_attempts` → `gave_up`, **unclaim all** + `needs-human` each, never
  re-pick. Below threshold → issues return to the pool (unclaimed) for a later retry.
- **Reconciliation sweep (restart-safety):** a periodic pass clears **dangling claims** — an
  `in-progress` label whose `batch.primary` has no live `origin_ref` session after a grace period →
  remove the label (return to pool) so a crash-between-claim-and-spawn can't leak a permanent claim.
  Records-before-labels (above) means the sweep knows the full claimed set to clean. Prefer re-claim
  over double-spawn; de-dup is the backstop.

### 5. Modes + config schema

Master `mode ∈ {off, propose, auto}`: `off` = idle; `propose` = select + claim, then surface the
picked `Batch` as a one-tap "Implement {primary}" approval (spawn on tap); `auto` = spawn
autonomously. Config (shell schema panel):

| key | type | default | meaning |
|---|---|---|---|
| `enabled_repos` | repo-multiselect | `[]` (all) | repos to draw issues from |
| `eligible_label` | string | `ready` | label gate for candidate issues |
| `max_concurrent` | int | `1` | implementation sessions in flight |
| `max_spawns_per_day` | int | `10` | circuit-breaker: pause spawning past this/day |
| `max_attempts` | int | `2` | per-issue give-up threshold (validated `>= 1`) |
| `max_issues_per_pr` | int | `1` | batch size (1 = one issue per PR) |

### 6. Status & activity

- `status(ctx)` (cheap; from the in-flight count + the **last tick's cached** candidate count + per-
  target state — never a live `gh` call on render): e.g. `"1 implementing, 5 eligible, 1 needs-human"`,
  level `active`/`warn`/`idle`. The loop bumps `backlog-runner` `last_active_at` on every tick that ran.
- Events (`ctx.emit(kind=…)`, issue/PR-tagged, session-linked): `selected` (`detail.issues`),
  `claimed`, `spawned`, `pr_opened`, `handoff`, `pr_doctor_recommended`, `circuit_open`,
  `skipped_no_account`, `gave_up`, `unclaimed`. The spawned session's `origin_ref`/`session_id` is the
  **pipeline join key** that stitches runner events to PR Doctor events for end-to-end debugging.

## Data flow

```
loop tick (60s / on runner-session end) — under a single lock (non-reentrant):
  mode off? → idle
  spawns_today >= max_spawns_per_day? → emit circuit_open → skip to sweep
  in_flight = sessions(origin="backlog-runner", status!="ended")
  free = max_concurrent - in_flight
  no healthy account? → emit skipped_no_account → skip to sweep
  repeat up to `free` (and under daily cap) while candidates remain:
    candidates = gh GraphQL (open, in enabled_repos, eligible_label, unassigned, unclaimed, not needs-human)
    batch      = rank_and_batch(candidates, max_issues_per_pr, one_shot_rank())   (pure + injected LLM)
    st = ctx.get_target_state(batch.primary); if st.gave_up → skip
    record claimed set → claim all (rollback on partial failure)
    mode auto    → create_session(origin first) → ctx.spawn_session(...); spawns_today++; emit spawned
    mode propose → surface "Implement {primary}"; (spawn on tap)
  sweep handed-off + in-flight targets:
    pr merged                                   → emit merged; delete state
    should_free_slot(pr, pr_doctor_active)      → emit handoff/merged; slot frees next tick
    pr-doctor gave_up(target) OR closed-unmerged → emit gave_up; unclaim all; needs-human; delete state
    session ended without mergeable PR          → attempts++; at max → gave_up + unclaim all + needs-human
  reconciliation: in-progress label with no live origin_ref session past grace → remove label
```

## Error handling

- **No healthy account** → never spawn; `skipped_no_account` (debounced); resume when one frees.
- **Circuit open** (daily cap) → no spawns; `circuit_open` once; sweep still runs; resets next day.
- **`gh` candidate query fails** → skip the tick (`warn`); retry next interval.
- **Claim partial failure** → roll back this batch's labels, abort pick (`warn`); never spawn
  unclaimed.
- **LLM rank fails/times out** → deterministic fallback order; `warn`; loop still progresses.
- **Spawn fails / de-dup hit** → de-dup returns the existing session (no double-spawn); a real spawn
  failure → `error`, unclaim batch, attempt not consumed.
- **Restart mid-flight** → in-flight recomputed from DB; non-reentrant lock + origin-first persistence
  close the spawn/persist race; reconciliation clears dangling claims.

## Acceptance criteria

1. The loop keeps ≤ `max_concurrent` runner sessions in flight; refills as slots free; idles at `off`;
   the **non-reentrant lock + origin-first persistence prevent double-spawn** under an interleaved
   tick+nudge (explicit test).
2. Selection: label-gated candidates only; ≤1 candidate skips the LLM; `>1` uses the injected
   one-shot rank (deterministic given a canned response); `max_issues_per_pr>1` → coherent batch ≤ cap;
   malformed model output → deterministic fallback.
3. Spawn carries `origin="backlog-runner"`, `origin_ref=primary`, batch in `detail`; shell de-dup
   prevents a second session for the same primary.
4. Handoff + terminal-watch: slot frees at PR-open+auto-merge when PR Doctor is active, else at merge;
   a handed-off PR that PR Doctor `gave_up` on (or closed-unmerged) → `needs-human` + state deleted
   (no claimed-forever leak).
5. Claim labels every batch issue (records-before-labels); partial-failure rolls back; give-up at
   `max_attempts` unclaims **all** + `needs-human` each; reconciliation clears a dangling claim.
6. Circuit-breaker: spawning pauses at `max_spawns_per_day` and resumes next day; `max_concurrent`
   default is 1; no spawn when no healthy account.
7. Server `pytest` green with **no network and no real sessions/quota**; the full loop suite runs on
   the fake-`gh`/fake-LLM/fake-spawn harness; `mode` defaults to `off`.
8. `auto_merge_enabled` is consumed from the shared field extension (this spec adds no `pr_status.py`
   query/parse/struct changes).

## Testing (TDD)

**Pure selection (`server/tests/test_backlog_selection.py`):** `rank_and_batch` (0/1/many; K=1 vs K;
canned order → expected `Batch`; malformed → fallback); `should_free_slot` ((pr open+auto_merge on/off)
× (pr_doctor_active) × merged).

**Loop integration (`server/tests/test_backlog_runner_loop.py`, fakes for gh/LLM/spawn):**
- Tops up to `max_concurrent`, no more; refills when a fake session ends with a mergeable PR.
- Interleaved tick+nudge mid-spawn → exactly one spawn (non-reentrancy).
- `off` idles; `propose` claims + surfaces but does not spawn; `auto` spawns.
- Claim-all; partial-claim rollback; give-up unclaims-all + `needs-human`; reconciliation clears a
  dangling claim.
- Terminal-watch: PR-Doctor `gave_up` for a handed-off target → `needs-human` + state deleted.
- Circuit-breaker pauses at the daily cap; no-account → `skipped_no_account`, no spawn; restart
  recomputes in-flight (no double-spawn); de-dup: two ticks/one primary → one session.

## Out of scope / YAGNI

- Full daily/budget accounting beyond the circuit-breaker.
- Cross-repo batching.
- Issue triage/curation, dependency-graph awareness, auto-creating `eligible_label`.
- A full triage *session* for ranking (one-shot query only).
- New persisted tables (rides the shell; consumes the shared `PullRequestInfo` field).

## Open implementation notes

- Build order: **shared PR-status field extension** → (PR Doctor plans) → **Runner Plan A**
  (loop/handoff/terminal-watch) → **Runner Plan B** (selection/claim/batching; Task 1 = fake harness).
  Runner Plan A may proceed once the field extension + PR Doctor exist (the handoff needs
  `pr_doctor_active` + its `gave_up` events).
- Keep `rank_and_batch` + `should_free_slot` pure; the loop is the only I/O boundary.
- Reuse: account selection + all-limited gate from the orchestrator; one-shot model call follows
  `generate_title`/`_default_title_fn`; candidate query uses `gitrepos` + `gh api graphql`; the
  implementation prompt leans on the repo's completion workflow (CLAUDE.md), not re-encoded.
- In-flight derivation from `Session.origin` (not process memory) is the key to restart-safety — never
  cache the in-flight set durably. The non-reentrant lock is single-process (orchestrator-owned).
- This spec adds **no** `pr_status.py` changes — `auto_merge_enabled` comes from the shared extension
  plan; the Runner only reads it.
