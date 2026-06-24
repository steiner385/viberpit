# Combined App — M1–M4 Implementation Plan (value-first)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) to implement this plan task-by-task, with review between tasks. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the three automations (Triage Lens, Work Dispatcher, PR Follow-through) on ccswitch's *current* PR data — no Plotroom engine port — per the adopted **Option A (value-first)** sequencing.

**Architecture:** All four milestones ride on two existing primitives, fixed/extended in place: the live-branch→session join (`_pr_pairs` / `worktrees.current_branch`) and the suggestion-chip surface (`latest_suggestions` + ghost text + Resume/chevron). No new poller, no parallel status vocabulary, no new DB tables in M1–M4 (Plotroom's engine + parity harness defer to M5).

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy(SQLite) backend (pytest/ruff/mypy); React + Vite + TS frontend (vitest / `tsc -b` / eslint).

## Global Constraints

- **No new runtime dependency** in M1–M4.
- **One PR poller.** Extend the existing `PrStatusPoller`; never add a second loop (Constitution VI).
- **Reuse, don't fork:** status vocabulary lives in `server/sessions/status.py` (`derive_status`, `ACTIVITY_STATUSES`); ordering tiers in `web/src/session/ordering.ts`; the proposal surface is `latest_suggestions` + ghost text. Extend these in place.
- **Autonomy = mixed by risk** (testable invariant): read flows auto-act; every write (launch a session, push a fix, merge) only *proposes*. AUT-04/11/12 are release-blocking; never skip/xfail them.
- **`PullRequestInfo` is RPC-serialized** (`server/sessions/remote.py`): any field/shape change must round-trip `to_dict`/`from_dict` and keep the daemon↔edge contract.
- **Nullable columns only** for any new persisted field (dev SQLite `init_db` backfills; no ALTER).
- **Backend test invocation:** `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/...`. **Frontend type gate is `tsc -b`**, never `tsc --noEmit`; run eslint with `--max-warnings 0`.
- **Plan structure note (writing-plans scope-check):** M1 below is full bite-sized TDD. **M2–M4 are task-level breakdowns** (files, interfaces, the catalog test-cases each satisfies, acceptance). Each is expanded to bite-sized steps in its own writing-plans pass **when M1's interfaces land**, because their concrete code depends on those interfaces (e.g. the live-branch helper M1 introduces is consumed by M3/M4 routing). This honors "each plan produces working, testable software on its own."

Test-case IDs (EST/STG/JOIN/AUT/…) reference `docs/superpowers/specs/2026-06-23-combined-app-user-stories-and-test-cases.md`.

---

# M1 — Live PR state on every session card (the live-branch fix)

**Why first:** the join is the load-bearing primitive for all three automations, and it is currently stale. `orchestrator._pr_pairs()` (server/sessions/orchestrator.py:817) keys PR lookups on the record's `r.branch`, which is derived from the live worktree (`worktrees.current_branch`) **only as a NULL-backfill** on rehydrate (orchestrator:710–715) and then frozen. When the completion workflow moves a worktree onto a fresh `feat/*` branch, the join goes stale and the PR pill silently disappears (JOIN-01/03; `pr-join-branch-must-be-live` memory — note that memory references a `worktrees.live_branch` symbol that does not exist in current code; the gap is real).

**Approach:** refresh each repo-backed record's `branch` from live worktree HEAD on each PR-poll sweep, off the event-loop thread (`asyncio.to_thread`), so `_pr_pairs` reads an up-to-date value. Make the poller's pairs-provider async-aware so the refresh runs immediately before each sweep.

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `_refresh_live_branches`; make `_pr_pairs` async)
- Modify: `server/github/pr_poller.py` (await the pairs-provider if it returns an awaitable)
- Test: `server/tests/test_pr_poller.py`, `server/tests/test_session_branch_persist.py` (new cases)

**Interfaces:**
- Consumes: `worktrees.current_branch(path) -> str | None` (git branch --show-current; None when detached), `worktrees.is_git_repo(path) -> bool`.
- Produces: `async SessionOrchestrator._refresh_live_branches() -> None` (updates `rec.branch` in place for repo-backed records when the live branch is non-None and changed); `async SessionOrchestrator._pr_pairs() -> list[tuple[str,str]]` (now async: refreshes then returns distinct pairs). `PrStatusPoller._loop` awaits the provider result if awaitable.

- [ ] **Step 1: Write the failing test (poller awaits an async provider)** — add to `server/tests/test_pr_poller.py`:

```python
@pytest.mark.asyncio
async def test_loop_accepts_async_pairs_provider():
    p = PrStatusPoller(query_fn=_good_query, poll_interval=0.01)

    async def async_pairs():
        return PAIRS

    p.set_pairs_provider(async_pairs)
    # Drive one iteration of the provider+refresh by calling the helper the loop uses:
    pairs = await p._resolve_pairs()
    assert pairs == PAIRS
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: _resolve_pairs`).

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_poller.py::test_loop_accepts_async_pairs_provider -v`

- [ ] **Step 3: Implement async-aware provider in `pr_poller.py`** — add a helper and use it in `_loop`:

```python
import inspect  # at top

    async def _resolve_pairs(self) -> list[tuple[str, str]]:
        if not self._pairs_provider:
            return []
        result = self._pairs_provider()
        if inspect.isawaitable(result):
            result = await result
        return result or []
```

Replace the body line in `_loop`:

```python
            pairs = await self._resolve_pairs()
            await self.refresh_once(pairs)
```

- [ ] **Step 4: Run it — expect PASS.** Also run the full `test_pr_poller.py` to confirm the sync-provider path still works (`_good_query` sync provider unaffected).

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_poller.py server/tests/test_pr_poller.py
git commit -m "feat(pr): poller pairs-provider may be async (enables live-branch refresh)"
```

- [ ] **Step 6: Write the failing test (orchestrator refreshes live branch)** — add to `server/tests/test_session_branch_persist.py`:

```python
@pytest.mark.asyncio
async def test_refresh_live_branches_updates_moved_worktree(orch, monkeypatch):
    # A record whose stored branch is stale; the worktree HEAD has since moved.
    rec = orch._records["s1"]            # repo-backed, cwd is a git worktree
    rec.repo, rec.branch = "o/r", "ccswitch/old-frozen"
    monkeypatch.setattr(orch_mod.worktrees, "is_git_repo", lambda p: True)
    monkeypatch.setattr(orch_mod.worktrees, "current_branch", lambda p: "feat/new-pr-branch")

    await orch._refresh_live_branches()

    assert rec.branch == "feat/new-pr-branch"            # JOIN-01/03
    assert ("o/r", "feat/new-pr-branch") in await orch._pr_pairs()

@pytest.mark.asyncio
async def test_refresh_live_branches_keeps_branch_when_detached(orch, monkeypatch):
    rec = orch._records["s1"]; rec.repo, rec.branch = "o/r", "feat/keep"
    monkeypatch.setattr(orch_mod.worktrees, "is_git_repo", lambda p: True)
    monkeypatch.setattr(orch_mod.worktrees, "current_branch", lambda p: None)  # detached
    await orch._refresh_live_branches()
    assert rec.branch == "feat/keep"                      # JOIN-02: don't clobber with None
```

(Use the module's existing `orch` fixture and `orch_mod` alias — both already used in this file at lines 134/150.)

- [ ] **Step 7: Run it — expect FAIL** (`AttributeError: _refresh_live_branches`; `_pr_pairs` not awaitable).

- [ ] **Step 8: Implement in `orchestrator.py`** — add the refresh and make `_pr_pairs` async:

```python
    async def _refresh_live_branches(self) -> None:
        """Re-read each repo-backed session's branch from its live worktree HEAD.

        The completion workflow moves a worktree onto a fresh PR branch after the
        record's branch was first set, so the stored value goes stale. Re-derive it
        off the event loop; only overwrite with a real (non-None) live branch.
        """
        for rec in list(self._records.values()):
            if not (rec.repo and rec.cwd):
                continue
            if not await asyncio.to_thread(worktrees.is_git_repo, rec.cwd):
                continue
            live = await asyncio.to_thread(worktrees.current_branch, rec.cwd)
            if live and live != rec.branch:
                rec.branch = live

    async def _pr_pairs(self) -> list[tuple[str, str]]:
        """Distinct (repo, live-branch) pairs for sessions that have both."""
        await self._refresh_live_branches()
        seen: set[tuple[str, str]] = set()
        for r in self._records.values():
            branch = getattr(r, "branch", None)
            if r.repo and branch:
                seen.add((r.repo, branch))
        return list(seen)
```

(`_pr_poller.set_pairs_provider(self._pr_pairs)` at line 170 now hands the poller an async provider — resolved by Step 3's `_resolve_pairs`.)

- [ ] **Step 9: Run the new tests — expect PASS.**

- [ ] **Step 10: Run the regression guard** — `test_session_branch_persist.py`, `test_pr_poller.py`, `test_remote_orchestrator.py`, `test_orchestrator_token_refresh.py` (the last mocks `_pr_poller`; confirm the async provider doesn't break it). Then ruff + mypy on `server/sessions/orchestrator.py` and `server/github/pr_poller.py`.

- [ ] **Step 11: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_session_branch_persist.py
git commit -m "fix(pr): key PR lookups on the live worktree branch, not the frozen stored branch (JOIN-01/02/03)"
```

- [ ] **Step 12: Manual verification (Principle III).** With a live session whose worktree you move onto a new branch (`git -C <wt> switch -c feat/x`), confirm within one poll interval that the session card's PR pill re-resolves to the PR on `feat/x` (or disappears correctly if none). Record the observation in the PR description.

**M1 acceptance:** JOIN-01/02/03/04/05 hold; PR pill follows the live branch; exactly one poller; no regression in branch-persist tests.

---

# M2 — Triage Lens (read-only → automatic) · *task-level; expand to bite-sized when M1 lands*

**Goal:** a single ranked "needs-me" attention queue across all sessions, fusing session status with PR state; push only for the top tier.

**Files (anticipated):**
- Backend: `server/sessions/triage.py` (new, pure ranking) ; `server/api/sessions.py` (attach a `triage` rank/tier to the list payload — same payload, no new hook, per the `dashboard-hooks-partial-mocks` trap).
- Frontend: `web/src/session/ordering.ts` (extend the existing status-tier rank to incorporate PR-broke); `web/src/components/SessionList.tsx` (consume the rank).
- Push: verify/extend the existing surface (see note).

**Interfaces (anticipated):**
- `triage_tier(status: str, pr: PullRequestInfo | None) -> int` — pure; lower = more urgent. Top tier = `status in {waiting_input, waiting_question, ready_review, needs_attention}` (the user is the blocker) ∪ (`pr.checks == "failing"`). Derives from the **existing** `derive_status` vocabulary (`server/sessions/status.py`) — do not introduce a parallel status set.
- A boolean `needs_push(prev_tier, new_tier)` gating top-tier-only push.

**Tasks (each → bite-sized later):**
1. **Pure `triage_tier` ranking** — table-test every status × PR-state combo. Tests: **AUT-03** (a `done` session with a `parked/ci-failed`/`failing` PR outranks an idle session with a green PR), plus a full truth table over the `status.py` vocabulary. *Reuse `ACTIVITY_STATUSES`; reconcile with `ordering.ts` tier list (the `#215` hardcoded-tier-drop trap — extend, don't fork).*
2. **Attach triage rank to the session-list payload** — no new frontend hook. Test: list payload carries a stable `triageTier`; AUT-01 (updates without approval).
3. **Frontend: order by triage tier** — extend `ordering.ts` `sortBy: 'status'` to use the fused tier. vitest: ranking renders crashed/needs-input/broken-PR first.
4. **Top-tier push** — fire a push only on transition *into* the top tier (waiting-on-you / crashed / PR-broke). Tests: **AUT-02** (push for top tier; none for lower), debounce once-per-condition. **PREREQUISITE SPIKE:** confirm whether a push/phone delivery path exists (grep showed no dedicated module). If none, M2's push slice becomes its own small build (web-push or a notification sink); the in-app ranking ships regardless (US-1 before US-3).

**M2 acceptance:** AUT-01/02/03; ranked list updates automatically; push only top-tier; no parallel status vocabulary introduced.

---

# M3 — Work Dispatcher chips (write → propose) · *task-level; expand when M2 lands*

**Goal:** when a session goes idle/done, propose the next task via the existing suggestion-chip surface; approve → launch pre-loaded with the matching skill; never auto-launch.

**Files (anticipated):**
- Backend: `server/sessions/dispatcher_sources.py` (new — candidate task generation); integrate with `server/sessions/suggestions.py` / `latest_suggestions` (extend in place — the `suggestion-prefill-done-idle` design is the foundation).
- Frontend: the existing Resume/chevron + ghost-text chip (no new surface — AUT-08).

**Interfaces (anticipated):**
- `async next_task_candidates(rec) -> list[TaskProposal]` where `TaskProposal = {source: 'issue'|'broken-pr'|'review', title, skill, target, why}`.
  - `issue` → top open GitHub issue via the existing `prioritize-issues`/`implement-loop` path (`gh` issue query).
  - `broken-pr` → this session's live-branch PR with `checks == "failing"` → `gh-fix`.
  - `review` → unresolved review comments on the PR → `pr-review`.
- Approval reuses the existing resume/launch path (the same one Resume/chevron drives) pre-loaded with `skill` + `target`.

**Tasks (each → bite-sized later):**
1. **Candidate sources** (pure where possible; `gh` calls injected for tests). Tests: **AUT-05** (sources ∈ {issue, broken-pr, review}; never a hand-curated backlog).
2. **Surface candidates through `latest_suggestions`** for detached idle/done sessions — extend the existing detached-generation path, not a parallel store (AUT-08). Tests: chip shows the proposal + `why`.
3. **Approve → launch pre-loaded; dismiss → no-op.** Tests: **AUT-04** (idle/done → chip, no launch), **AUT-06** (approve → launches with skill/target), **AUT-07** (dismiss → no side effect). These are release-blocking autonomy invariants.
4. *(P1, optional)* ranking + collision-avoidance (US-7/8): de-prioritize an issue already owned by a live session.

**M3 acceptance:** AUT-04/05/06/07/08; dispatch is one approval, never an auto-launch; reuses the existing chip surface.

---

# M4 — PR Follow-through (mixed): broke→fix chip + stall/ship push · *task-level; expand when M3 lands*

**Goal:** route PR break/stall/ship back to the owning session by live branch; badge/triage auto-update; push on break/stall/ship; a `gh-fix` proposal chip on break; never auto-push a fix or auto-merge.

**Files (anticipated):**
- Backend: a thin signal layer over the existing poller data — `pr.checks == "failing"` (break) is already available; add minimal `gh`-queried stall (`queue-blocked`) and ship (`prod-live`/merged-and-deployed; `PullRequestInfo.deployed` already exists) detection. Route via the M1 live-branch join (`pr_for` / `session_for_branch`).
- Frontend: reuse the M3 chip for the fix proposal; reuse M2 triage/badge for auto-updates.

**Interfaces (anticipated):**
- `follow_through_event(rec, pr_prev, pr_now) -> Event | None` — pure transition detector emitting `broke | stalled | shipped`.
- Routing: `session_for_branch(repo, live_branch)` (composes M1).

**Tasks (each → bite-sized later):**
1. **Transition detector** (pure). Tests: break on `passing/pending → failing`; ship on `merged → deployed`; stall on queue-blocked; debounce once-per-condition, re-fire after clear (mirrors NTF-01/05/06 semantics, thin slice — only `ci-failed`/`queue-blocked`/`prod-live`).
2. **Auto badge/triage update on event** (read side). Tests: **AUT-09** (auto update), **AUT-13** (routes by live branch; stale branch must not misroute — composes JOIN-01).
3. **Break → `gh-fix` proposal chip** (reuses M3). Tests: **AUT-11** (fix is a proposal, not an auto-push).
4. **Push on break/stall/ship** (reuses M2 push). Tests: **AUT-10**; ship is quiet by default unless promoted (US-11).
5. **Merge never auto-executes.** Test: **AUT-12** (release-blocking).

**M4 acceptance:** AUT-09/10/11/12/13; the most-wanted automation works on thin integration; no engine port required.

---

## Self-Review

**Spec coverage (Option A roadmap M1–M4 + requirements FR-1..FR-6):**
- FR-1 (unified PR/session state, live branch, one poller) → M1 (Tasks 1–12). ✓
- FR-2 (Triage read-auto, top-tier push) → M2. ✓
- FR-3 (Dispatcher propose-not-act, three sources, reuse chip) → M3. ✓
- FR-4 (Follow-through routing, push, fix-proposal, no auto-merge) → M4. ✓
- FR-6 (autonomy invariant) → AUT-04/07/11/12 are called out as release-blocking in M3/M4. ✓
- Deferred by design (Option A): parity harness + engine port + Pipeline view → M5 (not in this plan). Stated in Global Constraints. ✓

**Placeholder scan:** M1 has complete code in every step. M2–M4 are explicitly *task-level* (not placeholders) with a stated expansion trigger (M-N lands) and rationale — the one genuine unknown (does a push path exist?) is called out as a prerequisite spike in M2 Task 4, not hidden.

**Type consistency:** `_pr_pairs` async ↔ poller `_resolve_pairs` await (M1 Steps 3/8); `_refresh_live_branches` updates `rec.branch` consumed by `_pr_pairs`; `triage_tier`/`TaskProposal`/`follow_through_event` names are consistent across M2→M3→M4 references; `PullRequestInfo.checks`/`.deployed` reused (not redefined).
