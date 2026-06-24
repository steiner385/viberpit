# M2b.2 — PR-broke push (bite-sized)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. **Depends on M2b (#236, `server/sessions/push.py`) and M1 (#231, live-branch poller) being merged to master — branch off master once both are in.** Backend only.

**Goal:** When a session's PR transitions into a failing-CI state, fire one push (reusing the M2b sink), routed to the owning session by live branch, debounced once-per-condition — so a broken PR alerts the operator away from the desk, not just on the dashboard.

**Architecture:** The PR poller already computes fresh PR state each sweep and holds the prior state in its cache. Add a transition callback: the poller reports `(repo, branch, old_checks, new_checks)` when a tracked PR's `checks` change; the orchestrator's handler fires `push_mod.push` for the owning session when the new state is `failing` (and the old wasn't), debounced via a `_pushed_pr` set. Reuses M2b's `push.py` and mirrors M2b.1's debounce.

**Tech stack:** Python, pytest. No new dependency.

## Global Constraints
- Reuse `server/sessions/push.py` (the M2b sink) — do NOT add a second push path.
- One poller — extend the existing `PrStatusPoller.refresh_once`; no new loop.
- The poller stays generic: it emits a *change event*; the orchestrator decides to push (keeps session/push knowledge out of `server/github/`).
- Debounce (AUT-02 shape): fire once on transition INTO failing; clear when the PR leaves failing so a later re-break re-fires.
- Never crash the poll loop — a callback exception must be swallowed (the poller's refresh already swallows on its own failure path; guard the callback too).
- Backend tests via the clone venv + `PYTHONPATH=$PWD/src` (this worktree has no local `.venv`). Run ruff on the WHOLE tree (`ruff check src/ server/`) and mypy, not just changed files (CI lints the whole tree — the M2a/M2b lint failures came from test-file violations slipping past per-file lint).

Test-case IDs reference the combined-app user-stories-and-test-cases doc.

## Task 1 — poller emits PR-checks transitions

**Files:** modify `server/github/pr_poller.py`; test `server/tests/test_pr_poller.py`.

**Interfaces:**
- `PrStatusPoller.set_on_change(fn)` where `fn(repo: str, branch: str, old_checks: str | None, new_checks: str) -> Awaitable | None`.
- In `refresh_once`, BEFORE the `self._cache.pop(key,...)`/`update(fresh)` block, snapshot `old = {k: self._cache.get(k) for k in pairs}`. AFTER updating, for each key now in `fresh`, if `old[key]` is None or `old[key].checks != fresh[key].checks`, call `self._on_change(repo, branch, old_checks, new_checks)` (await if awaitable; wrap in try/except — a callback error must never fail the sweep).

- [ ] **Step 1 — failing test** `server/tests/test_pr_poller.py`: two sequential `refresh_once` calls with an injected `query_fn` that returns `checks: 'passing'` then `checks: 'failing'` for `("o/r","br")`; assert `set_on_change` callback is invoked with `("o/r","br","passing","failing")` on the second refresh and NOT on a no-change third refresh. Add a case: a callback that raises does not break `refresh_once` (still returns True).
- [ ] **Step 2 — RED.**
- [ ] **Step 3 — implement** the snapshot + diff + guarded callback in `refresh_once`; add `self._on_change=None` in `__init__` and `set_on_change`.
- [ ] **Step 4 — GREEN; ruff (whole tree) + mypy; rerun existing `test_pr_poller.py`.**
- [ ] **Step 5 — commit** (`feat(pr): poller emits PR-checks transition callback`).

## Task 2 — orchestrator pushes on transition into failing

**Files:** modify `server/sessions/orchestrator.py`; test `server/tests/test_orchestrator_pr_push.py`.

**Interfaces:**
- `__init__`: `self._pushed_pr: set[tuple[str,str]] = set()`; after constructing `_pr_poller`, `self._pr_poller.set_on_change(self._on_pr_change)`.
- `async def _on_pr_change(self, repo, branch, old_checks, new_checks)`:
  - if `new_checks == "failing"` and `(repo,branch) not in self._pushed_pr`: add it; find the owning record (`next((r for r in self._records.values() if r.repo==repo and r.branch==branch), None)`); if found, `await push_mod.push("ccswitch — PR broke", f"{rec.name}: {repo} CI failing")`.
  - elif `new_checks != "failing"`: `self._pushed_pr.discard((repo,branch))`.

- [ ] **Step 1 — failing test** `server/tests/test_orchestrator_pr_push.py` (use `_make_orch` + monkeypatch `orch_mod.push_mod.push` to capture calls, mirroring `test_orchestrator_push.py`): add a record for `("o/r","feat/x")`; call `await orch._on_pr_change("o/r","feat/x","passing","failing")` → one push fired; a second identical call → no repeat (debounce); `_on_pr_change(...,"failing","passing")` clears; a later failing fires again; a transition for a `(repo,branch)` with no matching session → no push, no crash.
- [ ] **Step 2 — RED.**
- [ ] **Step 3 — implement** `_pushed_pr` + `_on_pr_change` + the `set_on_change` wiring. Import `from server.sessions import push as push_mod` (consistent with M2b so the test can monkeypatch `orch_mod.push_mod.push`).
- [ ] **Step 4 — GREEN; ruff (whole tree) + mypy; rerun `test_orchestrator_push.py` + `test_pr_poller.py`.**
- [ ] **Step 5 — commit** (`feat(push): alert when a session's PR breaks CI (M2b.2)`).

## Out of scope
- Stall (`queue-blocked`) and ship (`prod-live`) push — those need pipeline-stage signals that don't exist until the engine port (M5). M2b.2 is CI-failure only (the signal ccswitch already has via `checks=='failing'`).
- Routing via a shared `session_for_branch` helper — inline the lookup here; extract a helper only when M3/M4 also need it (then reconcile to one).

## Self-Review
- AUT-02 debounce (once on entry, clear, re-fire) → Task 2 tests. ✓
- Reuse push sink / one poller / poller stays generic → Tasks 1–2 constraints. ✓
- Never crash the sweep (guarded callback) → Task 1. ✓
- Types: `set_on_change(fn)`, `_on_pr_change(repo,branch,old,new)`, `_pushed_pr: set[tuple[str,str]]`; reuses `push_mod.push`. ✓
