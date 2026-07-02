# Backlog eligibility + PR Doctor scope + archived-slot recycling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Backlog Runner report the true eligible-issue count, stop archived sessions from holding runner slots, and make PR Doctor recognize/act on every red PR in its configured repos (not just ccswitch-authored ones).

**Architecture:** Three independent changes to existing automations. Fix C recycles a slot when a spawned session is archived, by routing archival through the existing sweep (delete target state). Fix B replaces the batch-size `eligible` byproduct with a real candidate-count seam. Fix A points PR Doctor's sweep + count at the existing `PrPopulationPoller` (all open PRs per repo) unioned with authored pairs. No new pollers, no new concepts.

**Tech Stack:** Python 3, asyncio, SQLAlchemy (async SQLite), pytest. All loop I/O is injected via seams so tests need no network/sessions.

## Global Constraints

- Constitution VI (single source of truth): reuse `PrPopulationPoller` and `query_candidates`; do not add parallel PR scanners or candidate queries.
- New loop seams are OPTIONAL constructor kwargs (default `None` / safe fallback) so existing `BacklogRunnerLoop(...)` call sites and tests keep working.
- Do not change `pr_doctor_engine.classify()` — UNSTABLE-failing PRs stay non-actionable by design.
- Dev SQLite `init_db` only backfills nullable columns — no schema changes in this plan (none needed).
- Tests run from a fresh worktree via the main checkout's venv:
  `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/…`
  (or the local `.venv` if present). Auth-independent tests here need no allowlist.
- Verify gates before PR: `ruff check server/`, `mypy server/`, `pytest server/tests/`.

---

### Task 1: Fix C — an archived spawned session frees its runner slot  — DROPPED (superseded by PR #421)

> **NOT IMPLEMENTED.** PR #421 ("auto-archive a spawned session when it signals completion")
> merged after this plan was written and already delivers archived-slot recycling: it
> auto-archives completed backlog sessions and treats `archived` as terminal in
> `_backlog_session_ended`, so the sweep reclaims the slot. It deliberately charges an
> attempt / escalates to needs-human for a completed run with no PR — which the Fix C below
> would have contradicted. Implementing Fix C would be a parallel, conflicting implementation,
> so it was dropped. The task text is retained only as a record.


**Files:**
- Modify: `server/automations/backlog_runner.py` (constructor seam + `_session_alive` + `_sweep_one`)
- Modify: `server/sessions/orchestrator.py` (`_backlog_session_ended` ~1066; add `_backlog_session_archived`; wire seam ~302-317)
- Test: `server/tests/test_backlog_runner_loop.py`

**Interfaces:**
- Consumes: existing `session_ended: (session_id) -> bool` seam.
- Produces: new optional seam `session_archived: Callable[[str], Awaitable[bool]] | None` on `BacklogRunnerLoop`; orchestrator method `_backlog_session_archived(session_id) -> bool`.

**Behavior:** A session that is `archived` is terminal. When the sweep sees a target whose
spawned session is archived and there is **no** merged/closed PR resolution, it frees the slot
cleanly (delete target state, emit `archived`) — WITHOUT charging an attempt or `needs-human`
(archival is an explicit terminal decision, not a failed attempt). An `ended` (non-archived)
session keeps the existing attempt/give-up semantics.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_backlog_runner_loop.py`. Extend `build_loop` to accept and wire an
`archived` set (mirror the existing `session_ended` style) — add param `archived=None`, and inside:

```python
    async def session_archived(session_id):
        return session_id in (archived or set())
    # ...pass session_archived=session_archived to BacklogRunnerLoop(...)
```

Then the test:

```python
def test_archived_session_with_no_pr_frees_slot_without_needs_human(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            archived={"sess-1"},
        )
        # Spawn r#1, then stamp its target state with the (archived) session id.
        await loop.tick()
        assert len(rec.spawns) == 1
        await ctx.set_target_state("r#1", {
            "session_id": "sess-1", "repo": "r", "issues": ["r#1"], "claimed": True})
        # Next sweep: archived session, no PR -> slot freed, no needs-human, state gone.
        await loop.tick()
        assert rec.needs_humans == []
        assert (await ctx.get_target_state("r#1")).get("issues") is None

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_backlog_runner_loop.py::test_archived_session_with_no_pr_frees_slot_without_needs_human -v`
Expected: FAIL (target state still present / `session_archived` kwarg unknown).

- [ ] **Step 3: Add the seam to `BacklogRunnerLoop`**

In `server/automations/backlog_runner.py`, add the type alias near the others (~line 273):

```python
SessionArchived = Callable[[str], Awaitable[bool]]
```

Add the constructor kwarg (after `session_ended`, ~line 331) and store it:

```python
        session_archived: SessionArchived | None = None,
```
```python
        self._session_archived = session_archived   # (session_id) -> bool | None
```

- [ ] **Step 4: Treat archived as not-alive, and free cleanly in the sweep**

In `_session_alive` (~line 592), after the `_ended` check, add an archived probe so an archived
session reads as not-alive:

```python
        if self._session_archived is not None and sid is not None:
            try:
                if await self._session_archived(sid):
                    return False
            except Exception:  # pragma: no cover — a probe failure must not stall
                log.warning("backlog_runner.archived_probe_failed sid=%s", sid, exc_info=True)
```

In `_sweep_one` (~line 573), replace the final `if info is None and not await self._session_alive(st):`
block so an archived session frees the slot with no attempt/needs-human, while a merely-ended
session keeps the existing attempt/give-up path:

```python
        # No mergeable PR and the runner session is no longer alive.
        if info is None and not await self._session_alive(st):
            sid = st.get("session_id")
            archived = False
            if self._session_archived is not None and sid is not None:
                try:
                    archived = await self._session_archived(sid)
                except Exception:  # pragma: no cover
                    archived = False
            if archived:
                # Explicit terminal decision — free the slot, no attempt, no needs-human.
                await self._ctx.emit("info", f"{target} archived — freeing slot",
                                     kind="unclaimed", target=target)
                await self._ctx.delete_target_state(target)
                return
            attempts = st.get("attempts", 0) + 1
            if attempts >= config["max_attempts"]:
                await self._ctx.emit("warn", f"{target}: {attempts} attempts, no PR — giving up",
                                     kind="gave_up", target=target)
                await self._needs_human(target)
                await self._ctx.delete_target_state(target)
            else:
                new_st = dict(st)
                new_st["attempts"] = attempts
                new_st["_ended"] = True
                await self._ctx.set_target_state(target, new_st)
```

- [ ] **Step 5: Wire the orchestrator seam**

In `server/sessions/orchestrator.py`, add next to `_backlog_session_ended` (~line 1076):

```python
    async def _backlog_session_archived(self, session_id: str) -> bool:
        """True once the runner session *session_id* has been archived.

        Archival is an explicit terminal decision (status='archived'); the sweep
        frees the slot without charging an attempt or handing off to needs-human."""
        from sqlalchemy import select as _select
        async with db_session() as db:
            status = (await db.execute(_select(DbSession.status).where(
                DbSession.id == session_id))).scalar_one_or_none()
        return status == "archived"
```

Wire it into the loop constructor (~line 315, after `session_ended=`):

```python
            session_archived=self._backlog_session_archived,
```

- [ ] **Step 6: Run tests**

Run: `pytest server/tests/test_backlog_runner_loop.py -v`
Expected: PASS (new test + all existing loop tests).

- [ ] **Step 7: Commit**

```bash
git add server/automations/backlog_runner.py server/sessions/orchestrator.py server/tests/test_backlog_runner_loop.py
git commit -m "fix(backlog): recycle a runner slot when its spawned session is archived

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Fix B — Backlog "eligible" reports the true candidate count

**Files:**
- Modify: `server/automations/backlog_runner.py` (`_stash_counts` ~382; constructor seam; drop `_last_eligible`)
- Modify: `server/sessions/orchestrator.py` (add `_backlog_count_candidates`; wire seam)
- Test: `server/tests/test_backlog_runner_loop.py`

**Interfaces:**
- Produces: new optional seam `count_candidates: Callable[[], Awaitable[int]] | None` on
  `BacklogRunnerLoop`; orchestrator `_backlog_count_candidates() -> int`.
- The `status()` render at `backlog_runner.py:244` is unchanged (still reads `counts["eligible"]`).

**Behavior:** `eligible` reflects `len(query_candidates(config))` every tick, independent of free
slots. Best-effort: a query failure yields 0 and never breaks the tick. The old `_last_eligible`
batch-size high-water is removed (single source of truth for the concept).

- [ ] **Step 1: Write the failing test**

Extend `build_loop` to accept `candidate_count=None` and wire:

```python
    async def count_candidates():
        return candidate_count if candidate_count is not None else 0
    # pass count_candidates=count_candidates to BacklogRunnerLoop(...) only when candidate_count is not None
```

Test that a saturated runner (no free slots) still reports the real eligible count:

```python
def test_eligible_reflects_candidate_count_even_when_saturated(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            candidate_count=7,          # GitHub says 7 ready issues exist
        )
        await loop.tick()               # fills the single slot
        counts = getattr(ctx, "backlog_counts", {})
        assert counts.get("eligible") == 7
        assert counts.get("in_flight") == 1

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_backlog_runner_loop.py::test_eligible_reflects_candidate_count_even_when_saturated -v`
Expected: FAIL (`eligible` is 1 from batch size, or kwarg unknown).

- [ ] **Step 3: Add the seam**

In `server/automations/backlog_runner.py`, add the alias (~line 262 area):

```python
CountCandidates = Callable[[], Awaitable[int]]
```

Constructor kwarg + store (near `count_in_flight`):

```python
        count_candidates: CountCandidates | None = None,
```
```python
        self._count_candidates = count_candidates    # -> int (eligible backlog size)
```

Remove the `self._last_eligible = 0` line (~352) and the two writes at ~458 / ~460.

- [ ] **Step 4: Report the real count in `_stash_counts`**

Replace the `"eligible": self._last_eligible` line (~398) region so `_stash_counts` reads the seam:

```python
        eligible = 0
        if self._count_candidates is not None:
            try:
                eligible = await self._count_candidates()
            except Exception:  # pragma: no cover — a count read must not stall the tick
                eligible = 0
        setattr(self._ctx, "backlog_counts", {
            "in_flight": in_flight,
            "eligible": eligible,
            "needs_human": needs_human,
        })
```

- [ ] **Step 5: Wire the orchestrator seam**

In `server/sessions/orchestrator.py`, add:

```python
    async def _backlog_count_candidates(self) -> int:
        """Count of eligible, unclaimed OPEN issues in configured repos.

        The honest backlog gauge for the Automation card — independent of how many
        runner slots are free (a saturated runner still reports the real depth)."""
        from server.automations.backlog_github import query_candidates
        _mode, config = await self._backlog_read_config()
        try:
            cands = await asyncio.to_thread(query_candidates, config, gh_run=self._gh_run)
        except Exception:  # pragma: no cover — a gh failure must not break the tick
            log.warning("orchestrator._backlog_count_candidates_failed", exc_info=True)
            return 0
        return len(cands)
```

Wire into the constructor (~line 307, near `count_in_flight=`):

```python
            count_candidates=self._backlog_count_candidates,
```

- [ ] **Step 6: Run tests**

Run: `pytest server/tests/test_backlog_runner_loop.py -v`
Expected: PASS. Also run `pytest server/tests/test_backlog_runner_automation.py -v` (status text).

- [ ] **Step 7: Commit**

```bash
git add server/automations/backlog_runner.py server/sessions/orchestrator.py server/tests/test_backlog_runner_loop.py
git commit -m "fix(backlog): report true eligible-issue count instead of last batch size

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Fix A — PR Doctor scans all open PRs in configured repos

**Files:**
- Modify: `server/github/pr_status.py` (`_PR_NODE_FIELDS` ~216; `PullRequestInfo` dataclass ~98; `_to_info` ~517)
- Modify: `server/sessions/orchestrator.py` (add `_pr_doctor_scope_pairs`; repoint `_pr_doctor_sweep` ~1298 and `pr_doctor_actionable` ~2198)
- Test: `server/tests/test_pr_status.py` (head_ref parse) and `server/tests/test_pr_doctor_sweep.py`

**Interfaces:**
- Consumes: `self._pr_population.all() -> dict[str, tuple[PullRequestInfo, ...]]`; existing
  `_pr_pairs()`; PR Doctor `enabled_repos` config (`pr_doctor.py:25`, empty = all watched repos).
- Produces: `PullRequestInfo.head_ref: str` (the PR's head branch, now on every cached PR);
  `_pr_doctor_scope_pairs() -> list[tuple[str, str]]` used by both the sweep and the count.

**Behavior:** The watch set becomes the union of (a) every open PR in the population cache whose
repo is in PR Doctor's configured scope (empty scope = all watched repos), and (b) `_pr_pairs()`
(authored/archived coverage). `classify()` is unchanged. In `auto` mode this means recovery
sessions may now target human-authored red PRs — gated by the existing terminal-state no-op,
per-head-SHA dedupe, and propose/auto mode.

**Enabler:** `PullRequestInfo` has no branch field today, and neither GraphQL query fetches
`headRefName` — the per-branch poller only knows the branch because it *queried by* it, so
population PRs (keyed by repo only) have no branch to dispatch on. Task 3 first adds `head_ref` to
the shared node fields + dataclass + the single `_to_info` constructor, so BOTH query paths carry
the branch.

- [ ] **Step 1: Write the failing parse test**

In `server/tests/test_pr_status.py` (match its existing `_to_info`/`parse_pr_response` harness),
assert the parsed PR carries its head branch:

```python
def test_to_info_populates_head_ref():
    from server.github.pr_status import _to_info
    info = _to_info({"number": 5, "url": "u", "state": "OPEN", "headRefName": "feature/x"}, {})
    assert info.head_ref == "feature/x"
```

Run: `pytest server/tests/test_pr_status.py::test_to_info_populates_head_ref -v`
Expected: FAIL (`AttributeError: ... has no attribute 'head_ref'`).

- [ ] **Step 2: Add `head_ref` to the node fields, dataclass, and constructor**

In `server/github/pr_status.py`:

`_PR_NODE_FIELDS` (~216) — add `headRefName`:

```python
    "number url title state isDraft updatedAt mergedAt headRefName headRefOid mergeStateStatus mergeable\n"
```

`PullRequestInfo` dataclass (~111, near `repo`) — add:

```python
    head_ref: str = ""  # PR head branch name (headRefName); empty when unknown
```

`_to_info` (~524, near `head_sha=`) — add:

```python
        head_ref=pr.get("headRefName", ""),
```

Run: `pytest server/tests/test_pr_status.py -v`
Expected: PASS (new test + existing parse tests unaffected — new field is additive/optional).

- [ ] **Step 3: Add `_pr_doctor_scope_pairs`**

In `server/sessions/orchestrator.py`, near `_pr_pairs` (~1891):

```python
    async def _pr_doctor_scope(self) -> set[str] | None:
        """PR Doctor's configured repo scope; None means 'all watched repos'."""
        from sqlalchemy import select
        from server.db.models import Automation
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pr-doctor"))).scalar_one_or_none()
            repos = list((row.config or {}).get("enabled_repos") or []) if row else []
        except Exception:  # pragma: no cover — defensive
            repos = []
        return set(repos) or None

    async def _pr_doctor_scope_pairs(self) -> list[tuple[str, str]]:
        """(repo, branch) pairs PR Doctor should evaluate: every open PR in the
        population cache within the configured scope, UNION the authored pairs
        (so archived-session PRs and not-yet-populated repos stay covered)."""
        scope = await self._pr_doctor_scope()
        seen: set[tuple[str, str]] = set(await self._pr_pairs())
        for repo, prs in self._pr_population.all().items():
            if scope is not None and repo not in scope:
                continue
            for info in prs:
                if info.head_ref:
                    seen.add((repo, info.head_ref))
        return list(seen)
```

- [ ] **Step 4: Write the failing test**

In `server/tests/test_pr_doctor_sweep.py` (follow that file's orchestrator-construction harness),
add a PR to the population cache with NO authoring session and assert it is counted:

```python
def test_actionable_counts_population_only_failing_pr(...):
    # Build orchestrator per this file's existing harness.
    # Seed self._pr_population._cache = {"o/r": (failing_pr_info,)} with a PR that has
    # checks="failing", merge_state_status="BLOCKED", state="open", branch="feature".
    # No session authored it (self._records empty; _pr_pairs() -> []).
    actionable = await orch.pr_doctor_actionable()
    assert any(a["number"] == failing_pr_info.number for a in actionable)
```

Also assert an UNSTABLE-failing PR is NOT counted (classify unchanged).

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest server/tests/test_pr_doctor_sweep.py -k population_only -v`
Expected: FAIL (population-only PR not in `_pr_pairs`, so not counted).

- [ ] **Step 6: Repoint the sweep and the count**

In `_pr_doctor_sweep` (~1298) change:

```python
        pairs = await self._pr_pairs()
```
to
```python
        pairs = await self._pr_doctor_scope_pairs()
```

In `pr_doctor_actionable` (~2198) change:

```python
        pairs = await self._pr_pairs()
```
to
```python
        pairs = await self._pr_doctor_scope_pairs()
```

Both then resolve `info = self.pr_for(repo, branch)`; ensure the population PR is also reachable
via `pr_for`. If `pr_for` (the per-branch `PrStatusPoller` cache) does not contain population-only
PRs, add a fallback inside both loops: `info = self.pr_for(repo, branch) or self._pr_population_pr(repo, branch)`,
where `_pr_population_pr` scans `self._pr_population.cached(repo)` for a matching branch. Add:

```python
    def _pr_population_pr(self, repo: str, branch: str) -> "PullRequestInfo | None":
        for info in self._pr_population.cached(repo):
            if info.head_ref == branch:
                return info
        return None
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest server/tests/test_pr_doctor_sweep.py -v`
Expected: PASS (population-only failing PR counted; UNSTABLE still excluded).

- [ ] **Step 8: Commit**

```bash
git add server/github/pr_status.py server/sessions/orchestrator.py server/tests/test_pr_status.py server/tests/test_pr_doctor_sweep.py
git commit -m "fix(pr-doctor): scan all open PRs in configured repos, not just authored ones

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification + PR

- [ ] **Step 1: Run the gates**

```bash
ruff check server/
mypy server/
pytest server/tests/ -q
```
Expected: all green. (`mypy` is a required CI gate — do not skip.)

- [ ] **Step 2: Push and open the PR (non-draft, auto-merge)**

```bash
git push -u origin fix/backlog-eligible-pr-doctor-scope
gh pr create --title "fix: honest backlog eligibility, PR Doctor repo-wide scope, archived-slot recycling" \
  --body "$(cat <<'EOF'
Fixes three Automation-panel defects (see docs/superpowers/specs/2026-07-02-backlog-eligible-pr-doctor-scope-design.md):

- **Backlog "eligible"** now reports the true count of ready, unclaimed issues (was the last batch size, stuck at 0 while saturated).
- **Archived sessions** no longer hold a runner slot — the sweep recycles the slot on archival without charging an attempt or escalating to needs-human.
- **PR Doctor** now scans every open PR in its configured repos (via the existing PrPopulationPoller) instead of only ccswitch-authored branches, so it recognizes and (in auto) repairs any red PR. `classify()` UNSTABLE handling is unchanged.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

- [ ] **Step 3: Confirm CI is running and auto-merge is armed**

```bash
gh pr view --json number,isDraft,autoMergeRequest,statusCheckRollup -q '{n:.number,draft:.isDraft,automerge:.autoMergeRequest.enabledAt}'
```
Expected: `draft:false`, `automerge` non-null.

## Self-Review notes

- **Spec coverage:** Fix A → Task 3; Fix B → Task 2; Fix C → Task 1; UNSTABLE unchanged (asserted in Task 3 test); verification → Task 4. All spec sections covered.
- **Type consistency:** new seams `session_archived` / `count_candidates` used with identical names in loop + orchestrator; `_pr_doctor_scope_pairs` referenced consistently by both call sites; `PullRequestInfo.head_ref` defined in Task 3 Step 2 and consumed in Steps 3/6.
- **Resolved detail:** `PullRequestInfo` had no branch field and neither query fetched `headRefName`; Task 3 adds `head_ref` via the shared `_to_info` constructor so both query paths carry it. No remaining placeholders.
