# PR Doctor — Plan B: Orchestrator Execution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the pure PR Doctor engine (Plan A) into the live poll loop: per PR change, re-arm → classify → plan_recovery → execute (mechanical `gh` update-branch, session inject-or-spawn, propose surface, notify, give-up); extend poller reach to archived-session PRs; surface propose actions on the PR Doctor card.

**Architecture:** The shell's Task 9 already routes `_on_pr_change` through the `pr-doctor` `AutomationContext` + per-target state for the *failing-CI* loop-guard. This plan **generalizes that method** into a `_pr_doctor_step(info)` that delegates all decisions to `pr_doctor_engine` (Plan A) and handles all three v1 failure modes. The injection path reuses the existing `request_pr_fix`; mechanical recovery is a direct `gh` call; archived-session PRs are recovered via the shell's de-duped `ctx.spawn_session`. **Task 1 builds a fake-`gh`/fake-spawn harness so the whole layer is tested with no network and no real sessions.**

**Tech Stack:** Python 3 · FastAPI · `gh` via `gitrepos._run_gh` · React/TS (propose surface) · pytest/vitest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-pr-doctor-design.md` (§1-4, §7). Depends on Plan A (`pr_doctor_engine`) and the PR-status field extension.
- **Do NOT edit `pr_status.py`** (field extension owns it) and **do NOT re-implement the engine** (Plan A owns classify/plan_recovery/state).
- Reuse, don't duplicate: `request_pr_fix` / `build_pr_fix_prompt` (existing), `gitrepos._run_gh` (gh), `push_mod.notify` + the existing `_pushed_pr` debounce, `ctx.spawn_session` / `ctx.emit` / `ctx.get/set_target_state` (shell).
- Mechanical `gh` and `spawn_session` MUST be injectable (a constructor seam / attribute) so tests never hit the network or spawn real sessions.
- The `pr-doctor` row's `last_active_at` is bumped on every poll tick that ran (stalled-detection).
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`; web in `web/`: `./node_modules/.bin/{vitest run,tsc --noEmit,eslint}`.

---

## File Structure

- Modify: `server/sessions/orchestrator.py` — generalize `_on_pr_change`→`_pr_doctor_step`; add a mechanical-gh seam; bump `last_active_at`; generalize `request_pr_fix` to spawn when no live driver.
- Modify: `server/sessions/pr_fix.py` — add `build_conflict_fix_prompt`.
- Modify: `server/github/pr_poller.py` (+ orchestrator pairs-provider) — extend the polled pair set to open PRs authored via ccswitch, minus terminal targets.
- Create: `server/api/automations_actions.py` *(or extend `server/api/automations.py`)* — `POST /api/automations/pr-doctor/fix` (propose one-tap).
- Frontend: `web/src/components/AutomationCard.tsx` — an "Actionable PRs" propose section for pr-doctor.
- Tests: `server/tests/test_pr_doctor_step.py`, `server/tests/test_pr_doctor_poll_reach.py`, `web/src/components/AutomationCard.test.tsx` (extend).

---

### Task 1: Fake-gh / fake-spawn harness + `_pr_doctor_step` skeleton

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_pr_doctor_step.py` (create)

**Interfaces:**
- Produces: `_pr_doctor_step(info)` that reads `pr-doctor` mode+config, applies repo-scope gating, calls `apply_rearm`→`classify`→`plan_recovery`, dispatches to executor methods, writes state, emits events, bumps `last_active_at`. A `self._gh_run` seam (defaults to `gitrepos._run_gh`) injectable for tests.

- [ ] **Step 1: Read the current code first.** Read `_on_pr_change` (~L728-808 post-shell-migration) and `_pr_autofix_mode` (~L1237) and the `self._pr_doctor_ctx` construction (~L267-273). The shell already does: on `failing` + auto + live driver → mark target state `attempted` → `request_pr_fix` → emit. You are generalizing this, not starting from scratch.

- [ ] **Step 2: Write the failing test** (fakes for gh + spawn; assert dispatch, not real I/O)

```python
# server/tests/test_pr_doctor_step.py  (sketch — mirror existing orchestrator test setup)
# Build a SessionOrchestrator with fake_sdk_factory; monkeypatch self._gh_run to a recorder
# and self._pr_doctor_ctx.spawn_session to a recorder. Drive _pr_doctor_step(info) with a
# PullRequestInfo and assert the right executor fired + the right event kind emitted + state.
```
Cases to assert (one test each): `out_of_date`+auto → `self._gh_run` called with an `update-branch` argv, NO spawn, `mechanical_attempts==1`, `updated_branch` event; `failing_ci`+auto+live-driver → inject via `request_pr_fix` (no spawn); `failing_ci`+auto+no-driver → `spawn_session` once; `merge_conflict`+auto (default cfg) → propose (no gh, no spawn); mode `off` → only notify. Use the engine's pure functions as the oracle.

- [ ] **Step 3: Implement the seam + `_pr_doctor_step`.** In `__init__`, add `self._gh_run = gitrepos._run_gh` (a swappable attribute). Replace the body of `_on_pr_change` with: keep the `_pushed_pr` notification debounce; for the failing/transition handling, delegate to a new `async def _pr_doctor_step(self, repo, branch, info)` that:
  1. `mode = await self._pr_autofix_mode()`; load config from the `pr-doctor` row; bump `last_active_at` (best-effort).
  2. If `info` is terminal (`target_terminal`) → emit terminal + delete target state + return.
  3. Repo-scope gate: if `config["enabled_repos"]` is non-empty and `repo` not in it → return (treat as off for this repo).
  4. `target = f"{repo}#{info.number}"`; `st = await ctx.get_target_state(target)`; `st = apply_rearm(st, info)`.
  5. `fm = classify(info)`; emit `detected` once on entry to a failing class (debounced via state, like the shell does); `action = plan_recovery(fm, mode, config, st)`.
  6. Dispatch on `action` type → the executor methods (Tasks 2/3) or notify/propose; on a real Mechanical/SessionFix success, `st = record_attempt(st, action, info.head_sha)` and emit `attempted`/`updated_branch`.
  7. `await ctx.set_target_state(target, st)`.
> Keep the existing failing-CI behavior a strict subset (the shell's pr_autofix tests must still pass — run them).

- [ ] **Step 4: Run** `test_pr_doctor_step.py` + the existing `test_pr_autofix.py`/`test_orchestrator_pr_push.py`/`test_pr_autofix_migration.py` → all green.

- [ ] **Step 5: Commit** (`feat(pr-doctor): _pr_doctor_step dispatch via the pure engine + gh seam`).

---

### Task 2: Mechanical recovery — `gh` update-branch

**Files:** Modify `server/sessions/orchestrator.py`; Test extend `server/tests/test_pr_doctor_step.py`.

**Interfaces:** `async _pr_doctor_mechanical(repo, info, kind) -> bool` (runs the `gh` call via `self._gh_run`, returns success).

- [ ] **Step 1: Failing test** — `out_of_date`+auto fires `self._gh_run(["api","-X","PUT", f"repos/{repo}/pulls/{n}/update-branch"])` (assert argv), returns success → `mechanical_attempts` incremented, `updated_branch` event with `target`; a `self._gh_run` returning failure → attempt NOT consumed (re-arm), `warn` event, falls through to notify.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement** `_pr_doctor_mechanical`: build the update-branch argv, call `self._gh_run(argv)` (it returns a proc with `returncode`); on success emit `updated_branch` + return True; on failure emit `level="warn"` + return False. Wire it into `_pr_doctor_step`'s `Mechanical` branch (consume attempt only on True; `record_attempt` uses `info.head_sha` — note the new head appears on the *next* poll, which is correct for the self-push guard).
- [ ] **Step 4:** Run mechanical tests → green.
- [ ] **Step 5: Commit** (`feat(pr-doctor): mechanical update-branch recovery (no session)`).

---

### Task 3: Session recovery — inject-or-spawn + conflict prompt

**Files:** Modify `server/sessions/orchestrator.py`, `server/sessions/pr_fix.py`; Test extend.

**Interfaces:** generalized `request_pr_fix(session_id_or_none, repo, info, *, kind)` that injects into a live driver if present, else `ctx.spawn_session(origin_ref=f"{repo}#{info.number}", dedupe_key=…, …)` with a branch-aware first prompt; `build_conflict_fix_prompt(repo, pr)` in `pr_fix.py`.

- [ ] **Step 1: Failing test** — `failing_ci`+auto with a live driver for `(repo,branch)` → injects (existing path), no spawn; with NO live driver → `ctx.spawn_session` called once with the right `origin_ref`/`dedupe_key`, `attempted` event carries `session_id`; a second poll for the same target → de-dup (spawn recorder called once total). `merge_conflict`+auto+`auto_resolve_conflicts=True`+no-driver → spawn with the **conflict** prompt (assert `build_conflict_fix_prompt` text used).
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement.** Add `build_conflict_fix_prompt(repo, pr)` to `pr_fix.py` (branch-aware: `gh pr checkout`, rebase onto base, resolve conflicts, push — mirror `build_pr_fix_prompt`'s structure). Generalize the orchestrator's session path: resolve the owning live driver by `(repo, branch)`; if present, inject via the existing `send_prompt`/`request_pr_fix` with the kind-appropriate prompt; else `await self._pr_doctor_ctx.spawn_session(origin_ref=target, dedupe_key=target, repo=repo, account=<healthy account>, name=…)` and seed its first prompt with the kind-appropriate prompt. Consume the session attempt only on a real injection/spawn ("sent").
- [ ] **Step 4:** Run session-recovery tests + existing pr_fix tests → green.
- [ ] **Step 5: Commit** (`feat(pr-doctor): session recovery prefers live driver, else de-duped spawn; conflict prompt`).

---

### Task 4: Poller reach — watch archived-session open PRs, drop terminal

**Files:** Modify `server/github/pr_poller.py` (pairs-provider contract) + the orchestrator provider; Test `server/tests/test_pr_doctor_poll_reach.py` (create).

**Interfaces:** the orchestrator contributes `(repo, headRefName)` pairs for every **open** PR authored via ccswitch (from sessions' `authored_branches`, regardless of liveness), EXCLUDING branches whose `pr-doctor` target-state row is marked terminal.

- [ ] **Step 1: Failing test** — given a session (even archived/ended) with `authored_branches=["feat/x"]` and an open PR, the poller's pair set includes `(repo,"feat/x")`; once that PR's target is marked terminal (merged/closed), the provider stops yielding it.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement.** Read how `set_pairs_provider` is fed today (live-session branches). Add an additional source that enumerates open authored PRs from ALL sessions (not just live), and filter out branches whose target-state row has a terminal marker (the `_pr_doctor_step` terminate path sets it before deleting). Add a node-cost guard / chunking note to `build_pr_query` if the pair count exceeds a threshold (so a large set can't fail the whole poll).
- [ ] **Step 4:** Run reach tests + existing poller tests → green.
- [ ] **Step 5: Commit** (`feat(pr-doctor): poll archived-session open PRs; self-prune terminal targets`).

---

### Task 5: Propose surface — one-tap actions on the PR Doctor card

**Files:** Create/extend `server/api/automations.py` (a `POST .../pr-doctor/fix` action); `web/src/components/AutomationCard.tsx` (actionable-PRs section); `web/src/api/automations.ts` + hook; Tests backend + vitest.

**Interfaces:** `POST /api/automations/pr-doctor/fix` body `{repo, number, kind}` → routes through `request_pr_fix`/mechanical (gated to actionable PRs); a card section listing actionable PRs (from a small `GET .../pr-doctor/actionable` or reuse events) with kind-labeled buttons ("Update branch", "Resolve conflicts", "Fix CI").

- [ ] **Step 1: Failing tests** — backend: `POST /pr-doctor/fix` requires auth, 422 on bad kind, routes to the executor for an actionable PR, no-ops a terminal PR. Frontend: the section renders a button per actionable PR with the kind label and POSTs on click (MemoryRouter wrapper).
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement** the endpoint (reuse `_pr_doctor_step`'s executors, gated on actionable open/draft state) + the card section (a `usePrDoctorActions`/list hook). Use SPA nav / fetch; no bare `<a href>`.
- [ ] **Step 4:** Run backend + `./node_modules/.bin/{vitest run,tsc --noEmit,eslint}` → green.
- [ ] **Step 5: Commit** (`feat(pr-doctor): one-tap propose actions on the Automation card`).

---

## Self-Review

- **Spec coverage:** §2 detection-reach → Task 4; §3 router execution (mechanical vs session) → Tasks 2-3; §4 worker resolution → Task 3; §5 gating already in Plan A, applied here; §7 status `last_active_at` → Task 1; propose surface (§4) → Task 5. The engine (Plan A) is consumed, not duplicated.
- **Placeholders:** Task 1's test is a sketch (it must mirror the existing orchestrator test harness, which the implementer reads) — this is intentional scaffolding guidance, not a code placeholder; every other step names concrete argv/methods/events. The implementer reads `_on_pr_change` first (Step 1 of Task 1) because the exact code depends on the current post-shell orchestrator state.
- **Type consistency:** dispatches on the Plan A action dataclasses; `request_pr_fix`/`_pr_doctor_mechanical`/`spawn_session` signatures named consistently.

## Hand-off

After Plan B, PR Doctor is fully functional. The Backlog Runner plans (A: control loop/handoff/terminal-watch; B: selection/claim) depend on PR Doctor existing (the runner watches PR-Doctor `gave_up` events) and on `auto_merge_enabled` from the field-extension plan.
