# 003 Phase D — retire `in-progress` / `needs-human` labels in favour of board Status — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Backlog Runner stops writing the `in-progress` and `needs-human` labels; its claim is the durable target-state row, its give-up is a durable `needs_human_at` on that row, the reconciler turns both into board Status, and a one-time migration cleans the labels off open issues (Fixes #994).

**Architecture:** The runner already writes a durable claim row (`AutomationTargetState`, automation `backlog-runner`) BEFORE the label (records-before-labels), and the queue (C1) already excludes claimed rows and live sessions, so the `in-progress` label carries no signal any more — it dies with its whole GitHub-side lock machinery (claim/unclaim/heal/reconcile label paths and the label search behind them). The `needs-human` label carried two things: the give-up handoff and the retry cooldown. Both move onto the same target-state row (`gave_up: True, needs_human_at: <iso>`), which the runner keeps instead of deleting at give-up and expires itself once `needs_human_retry_days` has passed. The reconciler reads that row (`ItemFacts.needs_human_at`) and sets board Status = `Needs human`; the queue reads it for the cooldown skip. Humans see the claim and the handoff as issue comments (already posted today) and as board Status. A script migrates existing labels: runner-placed ones become rows, human-placed `needs-human` becomes a human Status edit on the board.

**Tech Stack:** Python 3.12 / SQLAlchemy 2.x (`AutomationTargetState.state` JSON) / `gh` CLI (REST for labels + timeline, GraphQL for the board field) / pytest with the existing recorder fakes.

**Spec:** `specs/003-gh-projects-integration/spec.md` §3 "What dies" and ownership matrix ("Issue body/title/labels: ccswitch writes never (one-time migration excepted)"), §7 deleted list (`in-progress`/`needs-human` label writes, labels cleaned up by migration), §6 (the reconciler is the only GitHub writer besides the migration). Issue #994 acceptance: no label writes remain in `server/automations/backlog_github.py`; labels removed from open issues; Status alone drives the queue. Finish-line row 9 (#1520), decision D11.

## Global Constraints

- Single sources of truth: skip reasons and ranking in `server/pm/queue.py`; the loader in `server/pm/queue_store.py:load_queue_rows(db, *, now, claimed_refs, needs_human_at)`; Status derivation in `server/pm/desired_state.py:derive_status`; reconciler inputs in `server/pm/reconcile_inputs.py`; the runner's durable state is `AutomationContext.{get,set,delete,list}_target_state` (JSON dict per target; `set_target_state` REPLACES the dict). Never add a second copy of any of these.
- Target-state row shape after this plan (all keys optional unless stated): `repo`, `issues` (list of refs, the batch), `claimed: True` while a session is being spawned/runs, `session_id`, `branch`, `attempts`, `proposed`, `handed_off`, and for a give-up: `gave_up: True` (required), `needs_human_at` (ISO-8601 UTC string, required), `reason` (str). A row with `gave_up` is NOT a claim: the queue must not count it as `claimed`.
- Cooldown rule (one definition, in `queue.skip_reason`): a row whose `needs_human_at` is set is skipped as `needs_human_cooldown` while `needs_human_retry_days <= 0` or `now - needs_human_at < retry_days`. The RUNNER expires rows: `_sweep` deletes a `gave_up` row once `now - needs_human_at >= retry_days` (never when `retry_days <= 0`); the reconciler then derives Status from the remaining facts and the queue offers the item again. "Any issue activity restarts the clock" (the old `gh_updated_at` rule) is dropped: a human comment must not delay a retry.
- Status derivation precedence stays `Done > session signals > needs-human > Blocked > Ready > Backlog`; `needs_human_at` replaces `needs_human_label` as the needs-human fact, with `decided_at = needs_human_at`. `triage_state == "needs_human"` keeps producing Needs human.
- No GitHub writes outside the reconciler except: issue COMMENTS the runner already posts (claim note, needs-human note, close-out note) and the one-time migration script. Comments are best-effort and never gate a spawn.
- Constitution VI: everything replaced is deleted in the same task — no `# deprecated` stubs. Deleted with this plan: `ensure_claim_labels`, `claim_batch` (→ `announce_claim`), `_rollback_claim`, `unclaim_batch`, `label_needs_human` (→ `announce_needs_human`), the label edits inside `_escalate_issue`, `_release_tracking_issue`, `close_batch_issues`, `heal_orphan_claims`, `reconcile_dangling`, `query_claimed_refs`, `_build_claimed_query`, `_ISSUE_NODES`, `_graphql_per_owner`; the loop's `claim`/`release`/`reconcile`/`heal_orphans` seams, `_orphan_sweep`, `_ORPHAN_SWEEP_INTERVAL`, `_last_orphan_sweep`, `orphan_*` events; the orchestrator's `_backlog_claim`, `_backlog_release`, `_backlog_reconcile`, `_backlog_heal_orphans`, `_backlog_batch_for` if unreferenced; `IN_PROGRESS_LABEL` / `NEEDS_HUMAN_LABEL` in `server/pm/queue.py` (the migration script carries its own two string literals — the only remaining reader).
- Tests: TDD per task; covering files named per task; the full `server/tests/` once at the end with the test-slot acquire in the same shell. Four-step gate before every push. `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`.
- Branch `feat/003-phase-d-label-retirement` off `origin/master`; one PR, body `Fixes #994`, `Refs #1520 #997`; commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in command text.
- Operator sequence after merge (runbook, Task 4): deploy → run the migration script dry-run → apply → confirm the reconciler stamps `Needs human` on the migrated rows.

## Decisions (controller, pre-execution — ledger them at execution time)

- D-1: A `gave_up` row persists (today it is deleted) and is the ONLY carrier of the needs-human signal. Cost if wrong: rows accumulate until expiry; bounded by `needs_human_retry_days`.
- D-2: The runner, not the queue or reconciler, expires cooldowns (`retry_days` is runner config). Cost if wrong: one sweep branch.
- D-3: Human-placed `needs-human` labels migrate to a human Status edit (`Needs human`) on the board, which the reconciler never overwrites; runner-placed ones (labeler login ends with `[bot]`) migrate to `gave_up` rows dated by the label event. Cost if wrong: re-run the script with `--only in-progress`.
- D-4: The claim comment and the needs-human comment stay (they are the human-visible trace the labels used to give); `close_batch_issues` gains the outcome `"escalated"` so the loop can keep a `gave_up` row instead of deleting it.
- D-5: Orphan healing from GitHub truth (`heal_orphan_claims`) dies with the label search. Residual risk (a lost DB row after a merged PR) is covered by the reconciler's `Done` from `merged_pr_at`; issue auto-close for that case is not replaced.

## File map

| File | Responsibility after this plan |
|---|---|
| `server/pm/queue.py` | `QueueRow.needs_human_at`; cooldown from it; label constants deleted |
| `server/pm/queue_store.py` | `load_queue_rows(..., needs_human_at: Mapping[str, datetime])` |
| `server/sessions/orchestrator.py` | `_backlog_query_queue` derives `claimed` (non-gave-up rows) and `needs_human_at` (gave-up rows); seams `announce_claim`, `announce_needs_human`; deleted seams removed |
| `server/automations/backlog_runner.py` | give-up writes the row; sweep expires it; `announce` seams; orphan sweep deleted; `wall_clock` injectable |
| `server/pm/desired_state.py`, `server/pm/reconcile_inputs.py` | `needs_human_at` fact from `AutomationTargetState` |
| `server/automations/backlog_github.py` | comments only; label machinery deleted; `close_batch_issues` → `"escalated"` |
| `scripts/retire_claim_labels.py` | one-time migration (dry-run default) |
| `docs/product/features/autopilot-backlog-runner.md`, `specs/003-gh-projects-integration/phase-d-runbook.md` | claim/handoff described; migration runbook |

---

### Task 1: Durable needs-human on the target-state row; queue cooldown reads it

**Files:**
- Modify: `server/pm/queue.py` (QueueRow, skip_reason, delete `IN_PROGRESS_LABEL`/`NEEDS_HUMAN_LABEL` — Task 3 deletes their last readers; in THIS task keep the constants but stop reading `NEEDS_HUMAN_LABEL` in `skip_reason`)
- Modify: `server/pm/queue_store.py` (`load_queue_rows` signature)
- Modify: `server/sessions/orchestrator.py` (`_backlog_query_queue`)
- Modify: `server/automations/backlog_runner.py` (`_sweep_one` give-up paths, `_sweep` expiry, `wall_clock` kwarg, `NeedsHuman` seam gains `reason`)
- Test: `server/tests/test_pm_queue.py`, `server/tests/test_pm_queue_store.py`, `server/tests/test_backlog_query_queue.py`, `server/tests/test_backlog_runner_loop.py`

**Interfaces:**
- Produces: `QueueRow.needs_human_at: datetime | None = None` (last field, defaulted). `load_queue_rows(db, *, now, claimed_refs=frozenset(), needs_human_at: Mapping[str, datetime] = {})`. `skip_reason` cooldown from `row.needs_human_at`. `BacklogRunnerLoop(..., needs_human: NeedsHuman | None, wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc))` with `NeedsHuman = Callable[[str, str], Awaitable[None]]` (target, reason). Give-up rows: `{"repo", "issues", "gave_up": True, "needs_human_at": iso, "reason"}`.

- [ ] **Step 1: Failing tests — queue cooldown from `needs_human_at`, label ignored** (`server/tests/test_pm_queue.py`; replace `test_needs_human_label_cools_down_by_gh_updated_at`):

```python
def test_needs_human_at_cools_down_then_reopens():
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    fresh = _row(needs_human_at=NOW - timedelta(days=2))
    assert skip_reason(fresh, needs_human_retry_days=7, **kw) == "needs_human_cooldown"
    assert skip_reason(fresh, needs_human_retry_days=0, **kw) == "needs_human_cooldown"  # 0 = never retry
    old = _row(needs_human_at=NOW - timedelta(days=8))
    assert skip_reason(old, needs_human_retry_days=7, **kw) is None


def test_needs_human_label_alone_no_longer_cools_down():
    """Phase D: the label is not a signal; only the runner's durable row is."""
    kw = dict(min_status="Backlog", respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(labels=("needs-human",), gh_updated_at=NOW), **kw) is None
```
(`_row` is the file's existing helper; extend it with `needs_human_at=None`.)

- [ ] **Step 2: Failing tests — loader threads the map** (`server/tests/test_pm_queue_store.py`, add):

```python
def test_load_queue_rows_threads_needs_human_at(db):   # reuse the file's seeded-mirror fixture
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = load_queue_rows(db, now=NOW, needs_human_at={"o/r#5": at})
    by_ref = {r.ref: r for r in rows}
    assert by_ref["o/r#5"].needs_human_at == at
    assert all(r.needs_human_at is None for ref, r in by_ref.items() if ref != "o/r#5")
```

- [ ] **Step 3: Failing tests — orchestrator derives both maps from rows** (`server/tests/test_backlog_query_queue.py`, add; reuse its `_orch`/seed helpers):

```python
def test_gave_up_row_is_a_cooldown_not_a_claim(tmp_path):
    async def run():
        orch = await _seeded_orch(tmp_path, items=[("o/r", 5, "Backlog")])
        await orch._backlog_ctx.set_target_state("o/r#5", {
            "repo": "o/r", "issues": ["o/r#5"], "gave_up": True,
            "needs_human_at": "2026-09-09T00:00:00+00:00", "reason": "max attempts"})
        skipped: dict = {}
        cands = await orch._backlog_query_queue({"enabled_repos": [], "queue_min_status": "Backlog",
                                                 "needs_human_retry_days": 7}, skipped_out=skipped)
        assert cands == [] and skipped == {"needs_human_cooldown": 1}
        cands = await orch._backlog_query_queue({"enabled_repos": [], "queue_min_status": "Backlog",
                                                 "needs_human_retry_days": 0}, skipped_out=skipped)
        assert skipped == {"needs_human_cooldown": 1}
    asyncio.run(run())
```
(Adapt to the file's fixtures: it already seeds products/boards and constructs the orchestrator via `__new__` + a real `AutomationContext("backlog-runner")`; freeze `now` by monkeypatching `datetime` is unnecessary — pick a `needs_human_at` far in the past for the "reopens" case: add a third call with `"needs_human_at": "2020-01-01T00:00:00+00:00"` and `retry_days=7` asserting one candidate.)

- [ ] **Step 4: Failing tests — runner keeps a `gave_up` row and expires it** (`server/tests/test_backlog_runner_loop.py`; `build_loop` gains `wall_clock=None` → passed through; existing give-up tests that assert `get_target_state(target) == {}` after give-up change to assert the row):

```python
def test_give_up_keeps_a_row_with_needs_human_at(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'g.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(candidates=[batch("o/r#7")], wall_clock=lambda: at,
                                    pr_status={}, config=make_config(max_attempts=1))
        await loop.tick()                          # spawn
        rec.in_flight = 0                          # session ended, no PR
        loop._session_ended_results = {}           # see file's existing "ended" fake
        await loop.tick()                          # attempts → 1 = max → give up
        st = await ctx.get_target_state("o/r#7")
        assert st["gave_up"] is True and st["needs_human_at"] == at.isoformat()
        assert st["issues"] == ["o/r#7"] and "max attempts" in st["reason"]
        assert rec.needs_humans == [("o/r#7", st["reason"])]
    asyncio.run(run())


def test_sweep_expires_gave_up_rows_after_retry_days(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(candidates=[], wall_clock=lambda: at + timedelta(days=8),
                                    config=make_config(needs_human_retry_days=7))
        await ctx.set_target_state("o/r#7", {"repo": "o/r", "issues": ["o/r#7"], "gave_up": True,
                                             "needs_human_at": at.isoformat(), "reason": "x"})
        await loop.tick()
        assert await ctx.get_target_state("o/r#7") == {}
        events = await ctx.list_events(kind="needs_human_expired")
        assert len(events) == 1
        # retry_days = 0 → permanent handoff: never expired.
        await ctx.set_target_state("o/r#8", {"repo": "o/r", "issues": ["o/r#8"], "gave_up": True,
                                             "needs_human_at": at.isoformat(), "reason": "x"})
        loop2, _, ctx2 = build_loop(candidates=[], wall_clock=lambda: at + timedelta(days=800),
                                    config=make_config(needs_human_retry_days=0))
        await loop2.tick()
        assert (await ctx2.get_target_state("o/r#8"))["gave_up"] is True
    asyncio.run(run())
```
Use the file's existing session-ended / give-up fakes (`session_ended`, `session_ran`) exactly as `test_terminal_watch_gave_up_unclaims_and_needs_human` does; the `needs_humans` recorder records `(target, reason)` tuples now.

- [ ] **Step 5: Run** the four files → FAIL (unknown kwargs / attributes).

- [ ] **Step 6: Implement.**

`server/pm/queue.py`:
```python
@dataclass(frozen=True)
class QueueRow:
    ...
    claimed: bool
    # The runner's durable give-up mark (Phase D): set while the issue waits for a human.
    needs_human_at: datetime | None = None
```
In `skip_reason`, BEFORE the `status` check (so the card counts it as a cooldown, not as a status skip):
```python
    if row.needs_human_at is not None and (
            needs_human_retry_days <= 0
            or now - row.needs_human_at < timedelta(days=needs_human_retry_days)):
        return "needs_human_cooldown"
```
and delete the `NEEDS_HUMAN_LABEL in row.labels` block. Update the `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` comment: "Escalation stamps `needs_human_at` on the runner's row; the runner deletes the row once the window passes, so a permanently failing issue is retried at most once per window."

`server/pm/queue_store.py`:
```python
def load_queue_rows(db: Session, *, now: datetime,
                    claimed_refs: AbstractSet[str] = frozenset(),
                    needs_human_at: Mapping[str, datetime] = {}) -> list[QueueRow]:
    ...
                live_session=ref in live_refs, claimed=ref in claimed_refs,
                needs_human_at=needs_human_at.get(ref),
```
(`Mapping` from `collections.abc`; a module-level `_NO_TIMES: Mapping[str, datetime] = {}` as the default avoids the mutable-default lint.)

`server/sessions/orchestrator.py` `_backlog_query_queue`:
```python
        states = await self._backlog_ctx.list_target_states()
        claimed = {ref for target, st in states.items()
                   if st.get("claimed") and not st.get("gave_up")
                   for ref in (st.get("issues") or [target])}
        needs_human_at = {ref: _iso(st["needs_human_at"])
                          for target, st in states.items()
                          if st.get("gave_up") and st.get("needs_human_at")
                          for ref in (st.get("issues") or [target])}
        ...
            rows = await db.run_sync(lambda s: load_queue_rows(
                s, now=now, claimed_refs=claimed, needs_human_at=needs_human_at))
```
with a module-level helper in `server/pm/queue_store.py` (single source, also used by the runner):
```python
def parse_iso_utc(value: str | None) -> datetime | None:
    """ISO-8601 → aware UTC datetime; None/garbage → None (a row we cannot read is no cooldown)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
```

`server/automations/backlog_runner.py`:
- `NeedsHuman = Callable[[str, str], Awaitable[None]]` (target, reason) — update the stub and the comment.
- `__init__(..., wall_clock: Callable[[], datetime] | None = None)` → `self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))`.
- New helper:
```python
    async def _give_up(self, target: str, st: dict, reason: str) -> None:
        """Hand the target to a human: announce it (comment, best-effort) and keep a
        durable ``gave_up`` row stamped ``needs_human_at``. The row is the ONLY carrier
        of the signal now (Phase D): the reconciler turns it into board Status
        ``Needs human`` and the queue into a cooldown; ``_sweep`` deletes it once
        ``needs_human_retry_days`` has passed so the issue is retried (never, when 0)."""
        await self._needs_human(target, reason)
        await self._ctx.set_target_state(target, {
            "repo": st.get("repo"),
            "issues": st.get("issues") or [target],
            "gave_up": True,
            "needs_human_at": self._wall_clock().isoformat(),
            "reason": reason,
        })
```
- Replace every `await self._needs_human(target)` + `await self._ctx.delete_target_state(target)` pair in `_sweep_one` (the "pr-doctor gave up / PR closed unmerged" branch and the attempts-exhausted branch) with `await self._give_up(target, st, reason)`; keep the `gave_up` emits.
- In `_sweep` (the loop over `states`), before dispatching to `_sweep_one`, expire:
```python
            if st.get("gave_up"):
                retry_days = int(config.get("needs_human_retry_days", NEEDS_HUMAN_RETRY_DAYS_DEFAULT))
                at = parse_iso_utc(st.get("needs_human_at"))
                if retry_days > 0 and at is not None and \
                        self._wall_clock() - at >= timedelta(days=retry_days):
                    await self._ctx.delete_target_state(target)
                    await self._ctx.emit("info", f"{target}: needs-human window passed — back in the pool",
                                         kind="needs_human_expired", target=target)
                continue
```
- `_stash_counts` `needs_human` count already sums `gave_up` rows — unchanged.
- The orchestrator's `_backlog_needs_human(target)` keeps working for now (Task 3 rewires it to a comment with the reason); give it the `(target, reason)` signature in this task and ignore `reason` until Task 3.

- [ ] **Step 7: Run** the four files + `server/tests/test_backlog_runner_e2e.py server/tests/test_pm_orchestrator_seams.py` → PASS; `ruff`, `mypy`.
- [ ] **Step 8: Commit** `feat(backlog): give-up is a durable row with needs_human_at; the queue cools down from it (Refs #994)`.

---

### Task 2: Reconciler derives `Needs human` from the runner's row, not the label

**Files:**
- Modify: `server/pm/desired_state.py` (`ItemFacts.needs_human_label: bool` → `needs_human_at: datetime | None`; `derive_status`)
- Modify: `server/pm/reconcile_inputs.py` (load `AutomationTargetState` rows for `backlog-runner`, map refs → `needs_human_at`)
- Test: `server/tests/test_desired_state.py`, `server/tests/test_reconcile_inputs.py`

**Interfaces:**
- Consumes: `parse_iso_utc` from Task 1; the row shape `{gave_up, needs_human_at, issues}`.
- Produces: `ItemFacts(needs_human_at=...)`; `derive_status` → `Decision(STATUS_NEEDS_HUMAN, f.needs_human_at)` after session signals, before Blocked.

- [ ] **Step 1: Failing tests.** In `test_desired_state.py`, `_f(...)` default `needs_human_at=None`; replace the label assertion:
```python
def test_status_needs_human_from_runner_row():
    assert derive_status(_f(needs_human_at=T1)) == Decision(STATUS_NEEDS_HUMAN, T1)
    # An active session outranks the give-up mark (the runner is retrying it).
    assert derive_status(_f(needs_human_at=T1, session_status="in_progress", session_at=T2)) \
        == Decision(STATUS_IN_PROGRESS, T2)
```
In `test_reconcile_inputs.py`, extend the fixture: insert an `AutomationTargetState(automation_id="backlog-runner", target="o/r#3", state={"gave_up": True, "needs_human_at": "2026-09-09T00:00:00+00:00", "issues": ["o/r#3", "o/r#4"]})` and assert `f3.needs_human_at == datetime(2026, 9, 9, tzinfo=timezone.utc)` and the same for `#4` (a bundle marks every issue in it); a row WITHOUT `gave_up` yields `None`. Delete the `needs_human_label is True` assertion and the `labels=["needs-human"]` seed it depended on.

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `desired_state.py`: rename the field; `if f.needs_human_at is not None or f.triage_state == "needs_human": return Decision(STATUS_NEEDS_HUMAN, f.needs_human_at or f.classified_at)`. `reconcile_inputs.py`:
```python
    rows = db.scalars(select(AutomationTargetState).where(
        AutomationTargetState.automation_id == "backlog-runner")).all()
    needs_human_at: dict[str, datetime] = {}
    for row in rows:
        st = row.state or {}
        at = parse_iso_utc(st.get("needs_human_at")) if st.get("gave_up") else None
        if at is None:
            continue
        for ref in (st.get("issues") or [row.target]):
            needs_human_at[ref] = at
```
and `needs_human_at=needs_human_at.get(ref)` in the `ItemFacts(...)` call; drop the `NEEDS_HUMAN_LABEL` import. Update the module docstring's precedence line to say "needs-human (runner row or triage)".
- [ ] **Step 4: Run** the two files + `server/tests/test_reconcile_convergence.py server/tests/test_gh_reconcile_loop.py` → PASS; `ruff`, `mypy`.
- [ ] **Step 5: Commit** `feat(reconcile): Needs human comes from the runner's give-up row, not the label (Refs #994)`.

---

### Task 3: Delete the label machinery; claims and handoffs are rows plus comments

**Files:**
- Modify: `server/automations/backlog_github.py`
- Modify: `server/automations/backlog_runner.py`
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/pm/queue.py` (delete `IN_PROGRESS_LABEL`, `NEEDS_HUMAN_LABEL`)
- Test: `server/tests/test_backlog_github.py`, `server/tests/test_backlog_runner_loop.py`, `server/tests/test_backlog_runner_e2e.py`, `server/tests/test_backlog_runner_codex_acceptance.py`, `server/tests/test_pm_orchestrator_seams.py`, `server/tests/fakes/fake_backlog.py` if it scripts label edits

**Interfaces:**
- Produces in `backlog_github.py`:
  - `announce_claim(batch, comment: str, *, gh_run) -> None` — posts `comment` on every issue in `batch.issues`; best-effort; logs and continues on failure. (Replaces `claim_batch`; no label, no rollback, no return value.)
  - `announce_needs_human(batch, reason: str, *, gh_run) -> None` — posts `f"Handed to a human by {brand.NAME} Backlog Runner: {reason}. Board Status is Needs human; the runner retries after the configured window."` on every issue. (Replaces `label_needs_human`.)
  - `close_batch_issues(batch, info, *, gh_run) -> str` returns `"closed" | "unlinked" | "retry" | "escalated"`; `"escalated"` when the human gate fired for at least one issue and none was closed; no label edits anywhere inside.
  - `_escalate_issue` and `_release_tracking_issue` post their comment only.
- Loop seams: `announce: Callable[[Batch], Awaitable[None]]` replaces `claim`; `release` seam DELETED (releasing is deleting the row, which the loop already does); `needs_human: Callable[[str, str], Awaitable[None]]`; `reconcile`, `heal_orphans`, `count_candidates`… unchanged except the two deletions. `_refill` calls `await self._announce(batch)` where it called `_claim` and never treats it as a race (no `continue`).
- Orchestrator: `_backlog_announce(batch)` → `announce_claim(batch, comment, gh_run=self._gh_run)`; `_backlog_needs_human(target, reason)` → `announce_needs_human(await self._backlog_batch_for(target), reason, gh_run=...)`; `_backlog_claim`, `_backlog_release`, `_backlog_reconcile`, `_backlog_heal_orphans` deleted and un-wired.

- [ ] **Step 1: Reader list.** `grep -rn "claim_batch\|unclaim_batch\|label_needs_human\|ensure_claim_labels\|query_claimed_refs\|heal_orphan_claims\|reconcile_dangling\|_ISSUE_NODES\|_graphql_per_owner\|IN_PROGRESS_LABEL\|NEEDS_HUMAN_LABEL\|_backlog_claim\|_backlog_release\|_backlog_reconcile\|_backlog_heal_orphans\|heal_orphans\|orphan_sweep" server/ src/ web/src docs/product scripts/` and write the list into the report before editing; every hit is deleted or re-pointed by the end of the task.
- [ ] **Step 2: Failing tests.**
  - `test_backlog_github.py`: delete the `ensure_claim_labels`, `claim_batch` (incl. rollback), `unclaim_batch`, `label_needs_human`, `query_claimed_refs`, `heal_orphan_claims`, `reconcile_dangling` sections and the two `_graphql_per_owner` failure-isolation tests (their subject dies). Add:
```python
def test_announce_claim_comments_every_issue_and_never_raises():
    gh = FakeGh({("issue", "comment", "9"): _proc(returncode=1, stderr="boom")})
    announce_claim(Batch(primary="o/r#7", issues=["o/r#7", "o/r#9"]), "Claimed by X", gh_run=gh)
    cmds = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
    assert [c[2] for c in cmds] == ["7", "9"] and all("--body" in c for c in cmds)
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)


def test_announce_needs_human_comments_with_reason():
    gh = FakeGh()
    announce_needs_human(Batch(primary="o/r#7", issues=["o/r#7"]), "max attempts", gh_run=gh)
    body = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    assert "max attempts" in body[body.index("--body") + 1]
    assert not any("label" in a for c in gh.calls for a in c)


def test_close_batch_issues_reports_escalated_and_edits_no_labels():
    # Reuse the file's existing human-gate fixture (action-required issue + merged PR linkage);
    # today's assertion checks the needs-human label was added — replace it:
    out = close_batch_issues(batch, info, gh_run=gh)
    assert out == "escalated"
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)
```
  - `test_backlog_runner_loop.py`: `build_loop` drops `claim_result`/`release` recorders and gains `announce` recorder (`rec.announced: list[Batch]`); delete `test_reconcile_deletes_dangling_claim_rows`' label half and every `orphan_sweep` test (`test_orphan_sweep_throttled_to_interval`, `..._passes_tracked_refs_and_emits_events`, `..._skipped_when_mode_off`, `..._runs_when_mode_propose`); the row-deletion reconciliation test keeps its row assertion. `test_propose_mode_deletes_state_row_on_claim_failure` is deleted (there is no claim failure). Add:
```python
def test_announce_failure_never_blocks_the_spawn(tmp_path):
    ...build_loop(candidates=[batch("o/r#7")], announce=raising_announce)
    await loop.tick()
    assert len(rec.spawns) == 1
```
  and a sweep test that a `close_out` returning `"escalated"` leaves a `gave_up` row with `needs_human_at` (reason `"human gate"`), not a deleted row.
  - `test_backlog_runner_e2e.py`: the `LabelTrackingGh` fake becomes `CommentTrackingGh` (records `issue comment` bodies); assertions on `in-progress` add/remove become: claim comment posted on spawn, needs-human comment posted on give-up, no label argv ever.
  - `test_backlog_runner_codex_acceptance.py`: replace `claim_batch`/`unclaim_batch` references with `announce_claim`; remove label assertions.
  - `test_pm_orchestrator_seams.py`: `_backlog_announce` threads the batch and gh seam into `announce_claim`; `_backlog_needs_human` threads `(batch, reason)`.
- [ ] **Step 3: Run** → FAIL (imports).
- [ ] **Step 4: Implement** — `backlog_github.py`:
```python
def announce_claim(batch, comment: str, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Human-visible trace of a claim: one comment per issue. The claim itself is the
    runner's durable target-state row (records-before-anything) plus board Status
    ``In progress`` from the reconciler — Phase D retired the ``in-progress`` label,
    so nothing here can fail a spawn: a comment that does not land is logged."""
    for ref in batch.issues:
        if not _issue_comment(ref, comment, gh_run=gh_run):
            log.warning("backlog_github.announce_claim_failed ref=%s", ref)


def announce_needs_human(batch, reason: str, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    body = (f"Handed to a human by {brand.NAME} Backlog Runner: {reason}. "
            "Board Status is Needs human; the runner retries after the configured window.")
    for ref in batch.issues:
        if not _issue_comment(ref, body, gh_run=gh_run):
            log.warning("backlog_github.announce_needs_human_failed ref=%s", ref)
```
  Delete: `ensure_claim_labels`, `claim_batch`, `_rollback_claim`, `unclaim_batch`, `label_needs_human`, `query_claimed_refs`, `_build_claimed_query`, `heal_orphan_claims`, `reconcile_dangling`, `_ISSUE_NODES`, `_graphql_per_owner` (keep `_group_repos_by_owner` — `verify_ready_live` uses it), the `IN_PROGRESS_LABEL`/`NEEDS_HUMAN_LABEL` import and every `_issue_edit(... add=/remove=...)` call; `_issue_edit` itself dies if no caller remains (check `grep`). `_escalate_issue` and `_release_tracking_issue` keep their comments. `close_batch_issues`: track `escalated = True` when `_human_gate_reason` fires; at the end `return "escalated" if escalated and not any_closed else "closed"`; docstring updated (the "would be excluded from the candidate pools forever" rationale is gone — say the close-out exists so a merged PR without a closing keyword still closes its issues). Module docstring: "claim lifecycle" → "claim announcements, needs-human handoff notes and close-out".
  `backlog_runner.py`: seam aliases (`Announce`, delete `Claim`, `Release`, `Reconcile`, `HealOrphans`), `__init__`, `_refill` (announce instead of claim in both modes; the propose-mode `if not await self._claim(batch): delete row; continue` block becomes just `await self._announce(batch)`), delete `_orphan_sweep`, `_ORPHAN_SWEEP_INTERVAL`, `_last_orphan_sweep`, the label half of `_reconcile_dangling` (keep the row deletion), the `release` calls on spawn failure (the row deletion stays), and the `_stub_claim`/`_stub_release` stubs. Sweep: on `close_out` outcome `"escalated"` → `await self._give_up(target, st, "human gate: merged PR needs a human close-out")`.
  `orchestrator.py`: `_backlog_announce`, `_backlog_needs_human(target, reason)`; delete `_backlog_claim`, `_backlog_release`, `_backlog_reconcile`, `_backlog_heal_orphans`; constructor kwargs updated. `server/pm/queue.py`: delete the two label constants and their comment.
- [ ] **Step 5: Run** the covering files + `server/tests/test_backlog_dispatch.py server/tests/test_backlog_query_queue.py server/tests/test_pm_queue.py`; `ruff` (dead imports surface here), `mypy`.
- [ ] **Step 6: Commit** `refactor(backlog): retire the in-progress/needs-human labels — claims are rows, handoffs are rows plus comments (Refs #994)`.

---

### Task 4: One-time migration script, runbook and feature doc

**Files:**
- Create: `scripts/retire_claim_labels.py`
- Create: `specs/003-gh-projects-integration/phase-d-runbook.md`
- Modify: `docs/product/features/autopilot-backlog-runner.md` (claim/handoff paragraph)
- Test: `server/tests/test_retire_claim_labels.py` (pure planning function over canned `gh` output)

**Interfaces:**
- `scripts/retire_claim_labels.py`: `python scripts/retire_claim_labels.py --repo steiner385/viberpit --repo cairnea/KinDash … [--apply] [--only in-progress|needs-human]`. Dry-run by default: prints one line per planned action. Pure core `plan_actions(open_issues: list[dict], timelines: dict[int, list[dict]]) -> list[Action]` with `Action(kind: Literal["strip_in_progress", "row_from_bot_label", "status_from_human_label"], ref: str, at: str | None, actor: str | None)`, unit-tested; the I/O shell runs `gh issue list --label X --state open --json number,labels`, `gh api repos/{repo}/issues/{n}/timeline` (filter `event == "labeled"` and `label.name`), and applies: `gh issue edit N --remove-label`, `AutomationContext("backlog-runner").set_target_state(ref, {...gave_up row, reason: "migrated from needs-human label"})` (needs `CCSWITCH_DB_URL` in the environment — the runbook says how), and for human-placed labels a GraphQL `updateProjectV2ItemFieldValue` on the product's board using the `Status` option id from `gh_projects.field_cache` (single-select option `Needs human`), then the label removal.

- [ ] **Step 1: Failing test** (`server/tests/test_retire_claim_labels.py`):
```python
from scripts.retire_claim_labels import plan_actions

def test_plan_actions_classifies_labels_by_actor():
    issues = [{"number": 1, "labels": [{"name": "in-progress"}]},
              {"number": 2, "labels": [{"name": "needs-human"}]},
              {"number": 3, "labels": [{"name": "needs-human"}, {"name": "in-progress"}]}]
    timelines = {
        2: [{"event": "labeled", "label": {"name": "needs-human"}, "actor": {"login": "viberpit[bot]"},
             "created_at": "2026-09-01T00:00:00Z"}],
        3: [{"event": "labeled", "label": {"name": "needs-human"}, "actor": {"login": "steiner385"},
             "created_at": "2026-09-02T00:00:00Z"}],
    }
    kinds = [(a.kind, a.ref) for a in plan_actions("o/r", issues, timelines)]
    assert kinds == [("strip_in_progress", "o/r#1"),
                     ("row_from_bot_label", "o/r#2"),
                     ("status_from_human_label", "o/r#3"), ("strip_in_progress", "o/r#3")]
    assert next(a for a in plan_actions("o/r", issues, timelines) if a.ref == "o/r#2").at == "2026-09-01T00:00:00+00:00"
```
- [ ] **Step 2: Run** → FAIL (no module). `scripts/` is not on the test path (verified: no conftest entry, no existing script tests), so the pure core lives in `server/pm/label_retirement.py` (`Action`, `plan_actions`) and both the test and `scripts/retire_claim_labels.py` import it from there; the test's import line is `from server.pm.label_retirement import plan_actions`.
- [ ] **Step 3: Implement** the pure core and the `gh`-driven shell (`--apply` gates every mutation; without it, print the plan and exit 0). Latest `labeled` event for the label decides the actor; no timeline event → treat as human (safer: Status edit, never a silent retry).
- [ ] **Step 4: Runbook** `phase-d-runbook.md`: preconditions (deployed daemon on this PR; reconciler in `apply`), the dry-run command per product repo, review, `--apply`, and verification (`GET /api/pm/queue` shows the migrated rows under `needs_human_cooldown`; boards show `Needs human` on human-migrated items; `gh issue list --label in-progress --state open` is empty in every product repo). Note that `needs-human` label DEFINITIONS are kept (humans may still use them; the runner no longer reads them) and `in-progress` definitions are deleted with `gh label delete` per repo after the sweep.
- [ ] **Step 5: Feature doc**: replace the "Claims are atomic `in-progress` labels…" sentence with the row + Status + comment description, and the give-up sentence with the `needs_human_at` row and its expiry.
- [ ] **Step 6: Commit** `feat(backlog): retire_claim_labels migration script + Phase D runbook (Refs #994)`.

---

### Task 5: Full gate, PR

- [ ] `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · full `pytest server/tests/ -n auto --dist loadfile --timeout=300` with the test-slot acquire in the same shell; classify any failure against master before touching it.
- [ ] PR: `Fixes #994`, `Refs #1520 #997`; body lists what died, the row shape, the operator sequence (deploy → migration dry-run → apply), and that #1524's orphan-heal item is moot (the heal died here). Ready + auto-merge per repo norm; the migration runs AFTER the deploy, by the operator or on their word.

## Self-review

- Spec coverage: §3 "what dies" (labels) → Task 3; ownership "labels: never, migration excepted" → Task 4; #994 acceptance "no label writes in backlog_github.py" → Task 3, "labels removed from open issues" → Task 4, "Status alone drives the queue" → Tasks 1–2 (queue reads rows and Status, never labels).
- Placeholder scan: none; every code step shows the code or the exact deletions with a grep.
- Type consistency: `needs_human_at` is `datetime | None` on `QueueRow` and `ItemFacts`, ISO string on the JSON row, parsed by one `parse_iso_utc`; `NeedsHuman = Callable[[str, str], Awaitable[None]]` in Tasks 1 and 3; `close_batch_issues` outcome `"escalated"` in Tasks 3 (producer) and 1/3 (sweep consumer).
