# Backlog Runner Orphan-Claim Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Backlog Runner from permanently locking issues out of its candidate pool via orphaned `in-progress` labels (22/25 KinDash issues are currently stuck this way).

**Architecture:** Two layers per the spec (`docs/superpowers/specs/2026-07-10-backlog-orphan-claims-design.md`): (1) the sweep keeps the target-state row alive through PR-Doctor handoff (`handed_off: True`) so the merged close-out always runs; (2) a throttled GitHub-truth orphan sweep queries open `label:in-progress` issues and heals any ref no state row tracks. All GitHub I/O stays behind the injected `gh_run` seam; all loop I/O stays behind injected async seams.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy(SQLite), pytest. No new dependencies.

## Global Constraints

- Never call the network or a real LLM in tests — every gh call goes through `FakeGh`, every loop dependency is an injected fake (constitution III/V).
- New/changed code must pass: `ruff check server/`, `mypy server/ --ignore-missing-imports`, and the server pytest suite.
- Test env (fresh worktree, no venv): run via system python with `PYTHONPATH=.:src`, e.g. `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_github.py -v`.
- Match existing docstring style: modules/functions explain the WHY (leak shape, invariants), not the mechanics.
- Commit after each task; messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: GitHub helpers — `query_claimed_refs` + `heal_orphan_claims`

**Files:**
- Modify: `server/automations/backlog_github.py` (after `close_batch_issues`, ~line 354)
- Test: `server/tests/test_backlog_github.py` (append)

**Interfaces:**
- Consumes: existing `_issue_edit`, `_split_ref`, `find_referencing_pr`, `IN_PROGRESS_LABEL`, `_ISSUE_NODES`, `gitrepos._run_gh`.
- Produces:
  - `query_claimed_refs(repos: list[str], *, gh_run: GhRun = gitrepos._run_gh) -> list[str]` — refs (`"owner/repo#N"`) of OPEN issues wearing `in-progress` across `repos`.
  - `heal_orphan_claims(refs: list[str], tracked: set[str], *, gh_run: GhRun = gitrepos._run_gh, limit: int = 20) -> list[tuple[str, str]]` — heals untracked refs; returns `(ref, "closed"|"reclaimed")` actions.
  - `_close_issue(ref: str, comment: str, *, gh_run: GhRun) -> bool` — single-issue close helper shared with `close_batch_issues`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_backlog_github.py` (reuses the module's `_proc` helper and `FakeGh`; add `query_claimed_refs`, `heal_orphan_claims` to the module's import list from `server.automations.backlog_github`):

```python
# --------------------------------------------------------------------------- #
# query_claimed_refs / heal_orphan_claims (orphaned-claim healing)            #
# --------------------------------------------------------------------------- #

def _claimed_stdout(refs: list[tuple[str, int]]) -> str:
    nodes = [{"number": n, "title": f"t{n}", "url": f"https://github.com/{r}/issues/{n}",
              "repository": {"nameWithOwner": r}} for r, n in refs]
    return json.dumps({"data": {"claimed": {"nodes": nodes}}})


def test_query_claimed_refs_builds_label_query_and_parses():
    fake = FakeGh({("api", "graphql"): _proc(stdout=_claimed_stdout([("o/r", 5), ("o/k", 9)]))})
    out = query_claimed_refs(["o/r", "o/k"], gh_run=fake)
    assert out == ["o/r#5", "o/k#9"]
    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "label:in-progress" in query
    assert "repo:o/r" in query and "repo:o/k" in query
    assert "is:open" in query


def test_query_claimed_refs_empty_repos_and_gh_failure_return_empty():
    assert query_claimed_refs([], gh_run=FakeGh()) == []
    fake = FakeGh({("api", "graphql"): _proc(returncode=1, stderr="boom")})
    assert query_claimed_refs(["o/r"], gh_run=fake) == []


def _crossref_stdout(prs: list[dict]) -> str:
    nodes = [{"source": {**pr, "repository": {"nameWithOwner": "o/r"}}} for pr in prs]
    return json.dumps({"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes}}}}})


def test_heal_orphan_merged_pr_closes_issue_and_strips_label():
    """An orphan whose referencing PR merged → close the issue + remove the claim."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout(
        [{"number": 40, "url": "https://github.com/o/r/pull/40", "title": "fix",
          "state": "MERGED", "isDraft": False, "headRefName": "b",
          "autoMergeRequest": None}]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == [("o/r#5", "closed")]
    close = next(c for c in fake.calls if c[:2] == ["issue", "close"])
    assert "--comment" in close and "o/r" in close
    edit = next(c for c in fake.calls if c[:2] == ["issue", "edit"])
    assert "--remove-label" in edit and "in-progress" in edit


def test_heal_orphan_open_pr_left_untouched():
    """An orphan with a live OPEN PR is real work — never unclaim or close it."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout(
        [{"number": 41, "url": "u", "title": "wip", "state": "OPEN",
          "isDraft": False, "headRefName": "b", "autoMergeRequest": None}]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == []
    assert not [c for c in fake.calls if c[:2] in (["issue", "edit"], ["issue", "close"])]


def test_heal_orphan_no_pr_reclaims_label():
    """No referencing PR (or closed-unmerged only) → stale lock; return to pool."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout([]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == [("o/r#5", "reclaimed")]
    edit = next(c for c in fake.calls if c[:2] == ["issue", "edit"])
    assert "--remove-label" in edit and "in-progress" in edit
    assert not [c for c in fake.calls if c[:2] == ["issue", "close"]]


def test_heal_orphan_skips_tracked_refs_and_honors_limit():
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout([]))})
    healed = heal_orphan_claims(["o/r#1", "o/r#2", "o/r#3"], {"o/r#1"},
                                gh_run=fake, limit=1)
    # #1 is tracked (skipped without a lookup); the cap allows exactly one heal.
    assert healed == [("o/r#2", "reclaimed")]
    assert len([c for c in fake.calls if c[:2] == ["api", "graphql"]]) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_github.py -v -k "claimed or heal"`
Expected: FAIL — `ImportError: cannot import name 'query_claimed_refs'`.

- [ ] **Step 3: Implement the helpers**

In `server/automations/backlog_github.py`, extract a single-issue close helper and rewrite `close_batch_issues` to use it (replace the current body's inline close loop):

```python
def _close_issue(ref: str, comment: str, *, gh_run: GhRun) -> bool:
    """Close one issue with a linking comment. Best-effort; returns success."""
    repo, number = _split_ref(ref)
    try:
        proc = gh_run(["issue", "close", number, "--repo", repo,
                       "--comment", comment])
        if getattr(proc, "returncode", 1) != 0:
            log.warning("backlog_github.close_issue_failed ref=%s", ref)
            return False
        return True
    except Exception:  # pragma: no cover — best-effort
        log.warning("backlog_github.close_issue_error ref=%s", ref, exc_info=True)
        return False


def close_batch_issues(batch, info, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Close every issue in ``batch`` after its PR merged, with a linking comment.

    The implement prompt asks for ``Fixes #N`` in the PR body, but sessions
    don't reliably comply — and an issue a merged PR fixed must not stay open
    wearing ``in-progress``/``needs-human`` (it would be excluded from the
    candidate pools forever). Best-effort: an individual failure is logged and
    the rest of the batch is still closed.
    """
    pr_link = getattr(info, "url", "") or f"PR #{getattr(info, 'number', '?')}"
    comment = (f"Implemented by {pr_link} (merged) — "
               f"closed by ccswitch Backlog Runner.")
    for ref in batch.issues:
        _close_issue(ref, comment, gh_run=gh_run)
```

Then append the two new functions:

```python
# --------------------------------------------------------------------------- #
# Orphaned-claim healing (GitHub truth)                                        #
# --------------------------------------------------------------------------- #

def query_claimed_refs(repos: list[str], *, gh_run: GhRun = gitrepos._run_gh) -> list[str]:
    """Refs of OPEN issues wearing ``in-progress`` across *repos* — GitHub's
    view of the runner's outstanding claims.

    The runner owns the ``in-progress`` label, so any labeled issue no target
    state tracks is an orphaned lock (crash, DB loss, or the pre-handed-off
    handoff leak) silently excluded from the candidate pools. This is the
    ground-truth read the heal pass diffs against. Failures return ``[]``.
    """
    if not repos:
        return []
    quals = [f"repo:{r}" for r in repos] + [
        "is:issue", "is:open", f"label:{IN_PROGRESS_LABEL}"]
    query = (
        "query {\n"
        f"  claimed: search(query: {json.dumps(' '.join(quals))}, type: ISSUE, first: 100) {{\n"
        f"    {_ISSUE_NODES}\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"])
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.claimed_query_failed err=%s",
                    (getattr(proc, "stderr", "") or "").strip())
        return []
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.claimed_query_unparseable", exc_info=True)
        return []
    nodes = (((payload.get("data") or {}).get("claimed") or {}).get("nodes")) or []
    out: list[str] = []
    for node in nodes:
        if not node:
            continue
        number = node.get("number")
        repo = (node.get("repository") or {}).get("nameWithOwner")
        if number is None or not repo:
            continue
        out.append(f"{repo}#{number}")
    return out


def heal_orphan_claims(refs: list[str], tracked: set[str], *,
                       gh_run: GhRun = gitrepos._run_gh,
                       limit: int = 20) -> list[tuple[str, str]]:
    """Heal claim labels no target state tracks. Returns ``(ref, action)`` pairs.

    Per untracked ref, the referencing PR decides (via
    :func:`find_referencing_pr`, same-repo only):

    - **merged** → the work landed but the close-out never ran (the handoff
      leak): close the issue with a linking comment and strip ``in-progress``
      → ``("closed")``.
    - **open/draft** → live work; leave the lock alone (a later pass heals it
      once the PR merges).
    - **none / closed-unmerged only** → a stale lock with nothing carrying it:
      strip ``in-progress`` so the issue re-enters the candidate pool
      → ``("reclaimed")``.

    ``limit`` caps cross-ref lookups per pass (each is a gh call — rate-limit
    hygiene; the hourly sweep catches the remainder next pass). Best-effort:
    an individual failure skips that ref and the pass continues.
    """
    healed: list[tuple[str, str]] = []
    looked_up = 0
    for ref in refs:
        if ref in tracked:
            continue
        if looked_up >= limit:
            break
        looked_up += 1
        try:
            info = find_referencing_pr(ref, gh_run=gh_run)
            if info is not None and info.state == "merged":
                pr_link = info.url or f"PR #{info.number}"
                comment = (f"Implemented by {pr_link} (merged) — closed by "
                           f"ccswitch Backlog Runner (orphaned-claim heal).")
                if _close_issue(ref, comment, gh_run=gh_run):
                    _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run)
                    healed.append((ref, "closed"))
            elif info is None:
                if _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run):
                    healed.append((ref, "reclaimed"))
            # open/draft → live work; leave untouched.
        except Exception:  # pragma: no cover — one ref must not stall the pass
            log.warning("backlog_github.heal_orphan_failed ref=%s", ref, exc_info=True)
    return healed
```

- [ ] **Step 4: Run the new tests + the whole module**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_github.py -v`
Expected: ALL PASS (including the pre-existing `close_batch_issues` test — the refactor must not change its argv behavior).

- [ ] **Step 5: Commit**

```bash
git add server/automations/backlog_github.py server/tests/test_backlog_github.py
git commit -m "feat(backlog): GitHub-truth claimed-ref query + orphan-claim heal helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Loop — track batches through handoff (`handed_off`)

**Files:**
- Modify: `server/automations/backlog_runner.py:605-631` (`_sweep_one` handoff + attempt branches)
- Test: `server/tests/test_backlog_runner_loop.py` (modify `test_handoff_frees_slot_next_tick`, add two tests)

**Interfaces:**
- Consumes: the loop harness `build_loop(...)` / `batch(...)` / `make_config(...)` already in the test file; `AutomationContext.get_target_state`.
- Produces: target-state rows now persist `{"handed_off": True}` after handoff instead of being deleted. Task 4 relies on this key name exactly: `handed_off`.

- [ ] **Step 1: Update the handoff test + write the two failing tests**

In `server/tests/test_backlog_runner_loop.py`, replace the body of `test_handoff_frees_slot_next_tick`'s post-first-tick section (the comment + assertions after `assert len(rec.spawns) == 1`) with:

```python
        # r#1's PR is open + auto_merge armed and pr-doctor covers it →
        # should_free_slot() frees the slot; the sweep marks the row handed_off
        # (it does NOT delete it — the close-out must still run when the PR
        # merges) and the next tick refills with r#2.
        rec.in_flight = 0  # orchestrator-side count excludes handed_off rows
        await loop.tick()
        assert len(rec.spawns) == 2
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True  # row survives handoff
        # A third tick must not re-emit handoff for the already-flagged row.
        await loop.tick()
        handoffs = await ctx.list_events(kind="handoff")
        assert len(handoffs) == 1
```

Then append two new tests:

```python
def test_merged_after_handoff_still_runs_close_out(tmp_path):
    """The handoff leak (KinDash lockout): a PR that merges AFTER handoff must
    still close out the batch's issues. The row survives handoff exactly so
    this path can run; only the merge deletes it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho1.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        pr_status = {"r#1": PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True)}
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            pr_status=pr_status,
            pr_doctor_active=True,
        )
        await loop.tick()   # spawn
        await loop.tick()   # handoff (row flagged, not deleted)
        assert (await ctx.get_target_state("r#1")).get("handed_off") is True
        assert rec.close_outs == []

        # The PR merges while handed off → close-out runs, row deleted.
        pr_status["r#1"] = PullRequestInfo(
            number=1, url="", state="merged", checks="passing", title="")
        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1"]
        assert await ctx.get_target_state("r#1") == {}

    asyncio.run(run())


def test_handed_off_row_never_charges_attempts(tmp_path):
    """After handoff the runner session has ended; a transient PR-lookup miss
    (info=None) must NOT burn attempts or escalate to needs-human."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho2.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        pr_status = {"r#1": PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True)}
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1, max_attempts=1),
            candidates=[batch("r#1")],
            pr_status=pr_status,
            pr_doctor_active=True,
        )
        await loop.tick()   # spawn
        await loop.tick()   # handoff
        # PR status blips to None (poller cache miss) for two sweeps.
        del pr_status["r#1"]
        await loop.tick()
        await loop.tick()
        assert rec.needs_humans == []
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True
        assert st.get("attempts", 0) == 0

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_runner_loop.py -v -k "handoff or handed"`
Expected: FAIL — `test_handoff_frees_slot_next_tick` (state row is `{}` after handoff), `test_merged_after_handoff_still_runs_close_out` (no close-out recorded), `test_handed_off_row_never_charges_attempts` (needs-human called after max_attempts=1).

- [ ] **Step 3: Implement `handed_off` in `_sweep_one`**

In `server/automations/backlog_runner.py`, replace the handoff block:

```python
        # PR reached the handoff point (PR Doctor carries it to auto-merge).
        if info is not None:
            active = await self._pr_doctor_active(repo)
            if should_free_slot(info, active):
                await self._ctx.emit("info", f"{target} handed off to PR Doctor",
                                     kind="handoff", target=target)
                await self._ctx.delete_target_state(target)
                return
```

with:

```python
        # PR reached the handoff point (PR Doctor carries it to auto-merge).
        # Mark the row handed_off instead of deleting it: the merged close-out
        # above must still run when the PR lands. Deleting here orphaned the
        # batch's ``in-progress`` labels whenever the merged PR lacked a
        # closing keyword — the labels never came off and the issues were
        # excluded from the candidate pools forever. The slot still frees now:
        # the orchestrator's in-flight count skips handed_off rows.
        if info is not None:
            active = await self._pr_doctor_active(repo)
            if should_free_slot(info, active):
                if not st.get("handed_off"):
                    new_st = dict(st)
                    new_st["handed_off"] = True
                    await self._ctx.set_target_state(target, new_st)
                    await self._ctx.emit("info", f"{target} handed off to PR Doctor",
                                         kind="handoff", target=target)
                return
```

and guard the attempt-charging branch (same method, just below):

```python
        # No mergeable PR and the runner session ended → count an attempt; give up
        # once we've burned through max_attempts. The attempt is charged HERE (on
        # a session ending without a mergeable PR), never at spawn time. A
        # handed_off row is exempt: its session is long gone by design, and a
        # transient PR-lookup miss must not read as "ended with no PR".
        if info is None and not st.get("handed_off") and not await self._session_alive(st):
```

- [ ] **Step 4: Run the loop test module**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_runner_loop.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/backlog_runner.py server/tests/test_backlog_runner_loop.py
git commit -m "fix(backlog): track batches through PR-Doctor handoff — close-out always runs

Deleting the target state at handoff orphaned the batch's in-progress labels
whenever the PR merged without a closing keyword; the issues stayed open,
label-excluded from the candidate pools forever (22/25 KinDash issues).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Loop — throttled orphan-heal seam

**Files:**
- Modify: `server/automations/backlog_runner.py` (seam alias ~line 295, `__init__` ~line 335, `_refill_and_sweep` ~line 385, new `_orphan_sweep` method)
- Test: `server/tests/test_backlog_runner_loop.py` (extend `build_loop`, add tests)

**Interfaces:**
- Consumes: `FakeClock` from the test harness; `_ctx.list_target_states()`.
- Produces: `BacklogRunnerLoop(..., heal_orphans=...)` keyword; seam signature `heal_orphans(config: dict, tracked: set[str]) -> Awaitable[list[tuple[str, str]]]`; module constant `_ORPHAN_SWEEP_INTERVAL = 3600.0`; feed events `orphan_closed` / `orphan_reclaimed`. Task 4 implements this exact seam on the orchestrator.

- [ ] **Step 1: Extend the harness + write the failing tests**

In `server/tests/test_backlog_runner_loop.py`:

1. Add a `heal_orphans_result=None` keyword to `build_loop(...)`'s signature.
2. Inside `Recorders.__init__`, add `self.heals: list = []`.
3. Next to the other seam fakes, add:

```python
    async def heal_orphans(config, tracked):
        rec.heals.append((config, set(tracked)))
        return list(heal_orphans_result or [])
```

4. Pass `heal_orphans=heal_orphans if heal_orphans_result is not None else None,` in the `BacklogRunnerLoop(...)` construction.

Then append the tests:

```python
def test_orphan_sweep_throttled_to_interval(tmp_path):
    """The heal pass runs at most once per _ORPHAN_SWEEP_INTERVAL — every tick
    would burn a gh search + up to `limit` cross-ref lookups."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os1.db'}"

    async def run():
        from server.automations.backlog_runner import _ORPHAN_SWEEP_INTERVAL
        await init_db(db_url=db_url)
        await _seed_row()
        clk = FakeClock()
        loop, rec, ctx = build_loop(clock=clk, heal_orphans_result=[])
        await loop.tick()
        await loop.tick()
        assert len(rec.heals) == 1  # second tick inside the window → skipped
        clk.advance(_ORPHAN_SWEEP_INTERVAL + 1)
        await loop.tick()
        assert len(rec.heals) == 2

    asyncio.run(run())


def test_orphan_sweep_passes_tracked_refs_and_emits_events(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            heal_orphans_result=[("o/r#7", "closed"), ("o/r#9", "reclaimed")])
        # A live batch row tracking two issues — both must be reported tracked.
        await ctx.set_target_state("o/r#1", {
            "claimed": True, "issues": ["o/r#1", "o/r#2"], "session_id": "s1"})
        await loop.tick()
        _config, tracked = rec.heals[0]
        assert {"o/r#1", "o/r#2"} <= tracked
        assert len(await ctx.list_events(kind="orphan_closed")) == 1
        assert len(await ctx.list_events(kind="orphan_reclaimed")) == 1

    asyncio.run(run())


def test_orphan_sweep_skipped_when_mode_off(tmp_path):
    """Mode off = the automation must take NO autonomous GitHub action —
    including healing (label writes are mutations)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off", heal_orphans_result=[])
        await loop.tick()
        assert rec.heals == []

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_runner_loop.py -v -k orphan`
Expected: FAIL — `TypeError: BacklogRunnerLoop.__init__() got an unexpected keyword argument 'heal_orphans'` (or ImportError for `_ORPHAN_SWEEP_INTERVAL`).

- [ ] **Step 3: Implement the seam + throttle**

In `server/automations/backlog_runner.py`:

1. Next to the other seam aliases (below `Reconcile`, ~line 295):

```python
# GitHub-truth orphan healing: (config, tracked_refs) -> [(ref, action), ...]
# where action is "closed" (merged PR found → issue closed) or "reclaimed"
# (stale lock released back to the pool).
HealOrphans = Callable[[dict, "set[str]"], Awaitable["list[tuple[str, str]]"]]

# The heal pass costs a gh search plus up to `limit` cross-ref lookups, so it
# runs at most once per interval — orphans are a slow leak, not a hot path.
_ORPHAN_SWEEP_INTERVAL = 3600.0
```

2. `__init__`: add keyword `heal_orphans: HealOrphans | None = None,` (after `close_out`), and in the body:

```python
        self._heal_orphans = heal_orphans               # GitHub-truth orphan healing
        self._last_orphan_sweep: float | None = None    # monotonic; throttles the heal pass
```

3. In `_refill_and_sweep`, after the `await self._reconcile_dangling()` line:

```python
        # GitHub-truth healing: claim labels NO state row tracks (crash, DB
        # loss, or pre-handed_off handoffs) are invisible to the row-derived
        # reconciliation above and lock issues out of the pools forever.
        if mode != "off":
            await self._orphan_sweep(config)
```

4. New method (after `_reconcile_dangling`):

```python
    async def _orphan_sweep(self, config: dict) -> None:
        """Heal orphaned claim labels from GitHub truth, at most once per
        ``_ORPHAN_SWEEP_INTERVAL``.

        Tracked refs are the union of every state row's ``issues`` — anything
        else wearing ``in-progress`` in the enabled repos is an orphan the
        injected seam heals (close if its PR merged, release if nothing carries
        it). The window stamp is taken up front so a failing pass also waits a
        full interval instead of hammering gh every tick."""
        if self._heal_orphans is None:
            return
        now = self._clock()
        if (self._last_orphan_sweep is not None
                and now - self._last_orphan_sweep < _ORPHAN_SWEEP_INTERVAL):
            return
        self._last_orphan_sweep = now
        try:
            states = await self._ctx.list_target_states()
            tracked: set[str] = set()
            for target, st in states.items():
                tracked.update(st.get("issues") or [target])
            healed = await self._heal_orphans(config, tracked)
        except Exception:  # pragma: no cover — healing must never break a tick
            log.warning("backlog_runner.orphan_sweep_failed", exc_info=True)
            return
        for ref, action in healed or []:
            if action == "closed":
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim — merged PR found, issue closed",
                    kind="orphan_closed", target=ref)
            else:
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim released back to the pool",
                    kind="orphan_reclaimed", target=ref)
```

- [ ] **Step 4: Run the loop test module**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_runner_loop.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/backlog_runner.py server/tests/test_backlog_runner_loop.py
git commit -m "feat(backlog): throttled GitHub-truth orphan-heal sweep in the runner loop

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Orchestrator wiring — heal seam + handed_off slot accounting

**Files:**
- Modify: `server/sessions/orchestrator.py:364-381` (loop construction), `:1156-1172` (`_backlog_count_in_flight`), `:1305-1318` (`_backlog_repo_load`), new `_backlog_heal_orphans` near `_backlog_reconcile` (~line 1368)
- Test: `server/tests/test_backlog_count_in_flight.py` (append)

**Interfaces:**
- Consumes: `backlog_github.query_claimed_refs(repos, gh_run=...)` and `backlog_github.heal_orphan_claims(refs, tracked, gh_run=...)` from Task 1; the `heal_orphans=` loop keyword and `handed_off` state key from Tasks 2–3.
- Produces: `SessionOrchestrator._backlog_heal_orphans(config: dict, tracked: set[str]) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_backlog_count_in_flight.py`:

```python
def test_handed_off_rows_do_not_hold_slots(tmp_path):
    """A handed_off row is kept only so the merged close-out can run — PR
    Doctor owns the PR now, so the row must not occupy a concurrency slot
    (or the runner would stall at max_concurrent handed-off rows)."""
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "session_id": "s1"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "session_id": "s2",
                                             "handed_off": True})
        assert await orch._backlog_count_in_flight() == 1

    asyncio.run(run())


def test_repo_load_skips_handed_off_rows(tmp_path):
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "repo": "o/r"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "repo": "o/r",
                                             "handed_off": True})
        await ctx.set_target_state("o/k#3", {"gave_up": True, "repo": "o/k"})
        assert await orch._backlog_repo_load() == {"o/r": 1}

    asyncio.run(run())


def test_heal_orphans_seam_queries_then_heals(tmp_path):
    """_backlog_heal_orphans = query_claimed_refs → heal_orphan_claims, both
    through the shared _gh_run seam; empty repo scope short-circuits."""
    async def run():
        import json as _json
        import subprocess

        await _init(tmp_path)
        orch = _orch_with_ctx()
        claimed = _json.dumps({"data": {"claimed": {"nodes": [
            {"number": 5, "title": "t", "url": "u",
             "repository": {"nameWithOwner": "o/r"}}]}}})
        crossref = _json.dumps({"data": {"repository": {"issue": {
            "timelineItems": {"nodes": []}}}}})
        procs = iter([
            subprocess.CompletedProcess([], 0, stdout=claimed, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=crossref, stderr=""),
        ])
        calls: list[list[str]] = []

        def gh_run(args, token=None):
            calls.append(list(args))
            if args[:2] == ["api", "graphql"]:
                return next(procs)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        orch._gh_run = gh_run
        healed = await orch._backlog_heal_orphans(
            {"enabled_repos": ["o/r"]}, set())
        assert healed == [("o/r#5", "reclaimed")]
        # Empty repo scope → no gh call at all.
        n = len(calls)
        assert await orch._backlog_heal_orphans({"enabled_repos": []}, set()) == []
        assert len(calls) == n

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_count_in_flight.py -v`
Expected: FAIL — `test_handed_off_rows_do_not_hold_slots` counts 2; `test_heal_orphans_seam_queries_then_heals` raises `AttributeError: _backlog_heal_orphans`.

- [ ] **Step 3: Implement the orchestrator changes**

In `server/sessions/orchestrator.py`:

1. `_backlog_count_in_flight` — change the return line and extend the docstring's final paragraph:

```python
        states = await self._backlog_ctx.list_target_states()
        return sum(1 for st in states.values()
                   if not st.get("gave_up") and not st.get("handed_off"))
```

Append to its docstring: `A handed_off row is PR Doctor's to carry — it is kept only so the merged close-out can run, and must not occupy a slot.`

2. `_backlog_repo_load` — in the loop body, change the skip line to:

```python
            if st.get("gave_up") or st.get("handed_off"):
                continue
```

3. New method next to `_backlog_reconcile` (~line 1368):

```python
    async def _backlog_heal_orphans(self, config: dict,
                                    tracked: set[str]) -> list[tuple[str, str]]:
        """GitHub-truth orphan healing: diff the open ``in-progress`` labels in
        the enabled repos against the refs the state rows track, and heal the
        rest (close if the referencing PR merged, release the claim if nothing
        carries it). Best-effort, off the event loop; the per-pass lookup cap
        lives in ``heal_orphan_claims``."""
        from server.automations import backlog_github
        repos = list(config.get("enabled_repos") or [])
        if not repos:
            return []
        refs = await asyncio.to_thread(
            backlog_github.query_claimed_refs, repos, gh_run=self._gh_run)
        if not refs:
            return []
        return await asyncio.to_thread(
            backlog_github.heal_orphan_claims, refs, tracked, gh_run=self._gh_run)
```

4. Loop construction (~line 380): add `heal_orphans=self._backlog_heal_orphans,` after `close_out=self._backlog_close_out,`.

- [ ] **Step 4: Run the affected test modules**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_backlog_count_in_flight.py server/tests/test_backlog_runner_loop.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_e2e.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_backlog_count_in_flight.py
git commit -m "feat(backlog): wire orphan healing + handed_off slot accounting into the orchestrator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full verification + PR

**Files:** none new — verification only.

- [ ] **Step 1: Lint + typecheck**

Run: `python3 -m ruff check server/ && python3 -m mypy server/ --ignore-missing-imports`
(If ruff/mypy aren't on the system python, use the main checkout's venv binaries.)
Expected: clean. mypy is a required CI gate — do not skip.

- [ ] **Step 2: Full server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ --ignore=server/tests/daemon_properties -q --deselect server/tests/test_eta_parity_live.py -k "not copilot_proxy_resolve"`
Expected: all pass (known-flaky exceptions per memory: `test_batch_unarchive_partial_failure`, `test_rearm_drain_snapshot` drain-cancel).

- [ ] **Step 3: Push + PR**

```bash
git push -u origin fix/backlog-orphan-claims
gh pr create --title "fix(backlog): heal orphaned in-progress claims; track batches through handoff" --body "$(cat <<'EOF'
## Problem
KinDash's backlog looked "skipped": 22/25 open issues wear the runner's own
`in-progress` label with no live work — `query_candidates` returns 0.

Two compounding bugs:
1. **Handoff leak** — the sweep deleted the target-state row when PR Doctor
   took over (auto-merge armed) while the PR was still open. When the PR later
   merged without `Fixes #N`, the close-out never ran: issues stayed open,
   label-excluded forever (e.g. cairnea/KinDash#11499, merged PR #11501).
2. **DB-blind reconciliation** — orphaned labels with no state row were
   invisible to `_reconcile_dangling`, so they accumulated monotonically.

## Fix
- Keep the target-state row through handoff (`handed_off: True`); the sweep
  keeps watching until merged (→ close-out) or closed-unmerged (→ needs-human).
  Slot accounting excludes handed-off rows, so concurrency is unchanged.
- New GitHub-truth orphan-heal sweep (hourly, capped): open `in-progress`
  issues no row tracks are closed (merged referencing PR) or released back to
  the candidate pool (no PR). Heals the 22 stuck KinDash issues on deploy.

Spec: docs/superpowers/specs/2026-07-10-backlog-orphan-claims-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR open (non-draft), auto-merge armed.
