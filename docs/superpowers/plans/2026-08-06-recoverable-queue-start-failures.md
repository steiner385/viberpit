# Recoverable Queue-Start Failures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Queued sessions that fail to start for a recoverable reason (auth 401, quota 429, no eligible account, unclassified transient) are re-enqueued as `queued` instead of being terminally `ended`, with account-quarantine gating, a same-pass guard, an attempt cap for unclassified transients, and worktree/X11 cleanup on every failed start.

**Architecture:** All changes live in `server/sessions/orchestrator.py`'s queue drain path plus one new parameter on `server/sessions/session_queue.py:next_entry`. A module-level classifier maps a start exception to `permanent | auth | quota | all_busy | transient`; the drain loop dispatches on that. Two orchestrator helpers carry the terminal (`_end_failed_start`) and re-enqueue (`_reenqueue_failed_start`) policies. Spec: `docs/superpowers/specs/2026-07-22-recoverable-queue-start-failures-design.md`.

**Tech Stack:** Python 3 / asyncio / SQLAlchemy async (SQLite), pytest.

## Global Constraints

- Run server tests with: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <file> -q` from the repo root. There is no project venv — system `python3` + `PYTHONPATH=.:src` is the supported invocation.
- Lint/type gates (CI-required): `python3 -m ruff check server/ src/` and `python3 -m mypy server/ --ignore-missing-imports`.
- No new session status; `queued` is reused (design non-goal).
- No DB schema change; `_queue_start_attempts` is in-memory only.
- Every task ends with a commit on the current branch `fix/recoverable-queue-start-reenqueue`.
- Existing symbols this plan builds on (all already imported in `orchestrator.py`): `BranchSwitchError`, `worktrees` (module `ccswitch.worktrees`), `AllAccountsBusy` / `select_account` (from `server.sessions.selection`), `RecoveryState` instance `self._recovery`, `QUARANTINE_401` / `QUARANTINE_429` (from `server.sessions.recovery`), `self._mark_needs_reauth(account)`, `self._clock()` (returns `time.time()` float), `db_session`, `DbSession`.
- Execution amendment (discovered during implementation, verified against `server/sessions/recovery.py`): `RecoveryState.quarantine()` has max()/extend-only semantics — a test cannot lower an existing future quarantine by calling `quarantine(until=<past>)`. Tests that need a lapsed quarantine set `orch._recovery._quarantine["alpha"] = orch._clock() - 1` directly.

---

### Task 1: `next_entry(exclude=...)` in session_queue

**Files:**
- Modify: `server/sessions/session_queue.py` (the `next_entry` function)
- Test: `server/tests/test_session_queue_model.py` (append)

**Interfaces:**
- Produces: `async def next_entry(exclude: Collection[str] = ()) -> SessionQueue | None` — returns the highest-priority queue entry whose `session_id` is NOT in `exclude`, or `None`. Task 4's drain loop passes its `attempted` set here; that single mechanism implements both the same-pass guard and gated-skip advancement.

Test (append to `server/tests/test_session_queue_model.py`):

```python
def test_next_entry_exclude_advances_past_head(tmp_path):
    """next_entry(exclude=...) skips excluded ids and returns the next in order;
    excluding everything returns None. This powers the drain loop's same-pass
    guard AND its account-gated skip (leave head queued, try the next entry)."""
    from server.db import init_db
    from server.sessions import session_queue as sq

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        await sq.enqueue("s1", {"name": "one"})
        await sq.enqueue("s2", {"name": "two"})
        head = await sq.next_entry()
        assert head is not None and head.session_id == "s1"
        nxt = await sq.next_entry(exclude={"s1"})
        assert nxt is not None and nxt.session_id == "s2"
        assert await sq.next_entry(exclude={"s1", "s2"}) is None
        # Default (no exclusion) is unchanged.
        assert (await sq.next_entry()).session_id == "s1"

    asyncio.run(run())
```

Implementation (replace `next_entry` in `server/sessions/session_queue.py`; add `from collections.abc import Collection` to imports):

```python
async def next_entry(exclude: Collection[str] = ()) -> SessionQueue | None:
    """Highest-priority entry not in ``exclude`` (or None).

    ``exclude`` lets the drain loop advance past entries it has already
    attempted this pass (same-pass guard) or is deliberately skipping
    (account-gated), without removing them from the queue.
    """
    async with db_session() as db:
        stmt = select(SessionQueue)
        if exclude:
            stmt = stmt.where(SessionQueue.session_id.not_in(list(exclude)))
        return (await db.execute(
            stmt.order_by(*_ORDER).limit(1))).scalar_one_or_none()
```

---

### Task 2: `_classify_start_failure` classifier

**Files:**
- Modify: `server/sessions/orchestrator.py` (module-level function directly below the `BranchSwitchError` class)
- Test: `server/tests/test_session_queue_drain.py` (append)

**Interfaces:**
- Consumes: `server.sessions.failure.classify(code, message) -> TurnFailure` (existing; `TurnFailure.kind` ∈ `{"rate_limit","auth_401","process_crash","turn_dropped","overloaded","other"}`), `BranchSwitchError`, `ccswitch.worktrees.WorktreeError`, `server.sessions.selection.AllAccountsBusy`.
- Produces: `def _classify_start_failure(exc: BaseException) -> str` returning exactly one of `"permanent" | "auth" | "quota" | "all_busy" | "transient"`. Task 4 dispatches on these five strings.

Test:

```python
# ---------------------------------------------------------------------------
# Failure classification (pure) — recoverable queue-start failures
# ---------------------------------------------------------------------------

def test_classify_start_failure():
    from ccswitch import worktrees
    from server.sessions.orchestrator import BranchSwitchError, _classify_start_failure
    from server.sessions.selection import AllAccountsBusy

    assert _classify_start_failure(BranchSwitchError("branch busy")) == "permanent"
    assert _classify_start_failure(worktrees.WorktreeError("git worktree add failed")) == "permanent"
    assert _classify_start_failure(AllAccountsBusy("no account has headroom")) == "all_busy"
    assert _classify_start_failure(
        RuntimeError("API error: 401 invalid authentication credentials")) == "auth"
    assert _classify_start_failure(RuntimeError("rate limit exceeded (429)")) == "quota"
    # Anything unrecognised defaults to a capped transient, never permanent.
    assert _classify_start_failure(RuntimeError("boom")) == "transient"
    assert _classify_start_failure(OSError("No space left on device")) == "transient"
```

Implementation:

```python
def _classify_start_failure(exc: BaseException) -> str:
    """Classify a _spawn_session failure for the queue drain loop.

    Returns one of:
      - "permanent": bad branch / worktree creation failed — retry cannot heal it.
      - "auth":      start-time 401 (dead/expired creds) — self-heals on re-auth.
      - "quota":     start-time 429 — self-heals when the window resets.
      - "all_busy":  no eligible account right now (quarantine/limits).
      - "transient": anything else — recoverable but bounded by the attempt cap.

    Reuses the live-turn failure taxonomy (server.sessions.failure.classify)
    for the 401/429 text inference so start-time and in-turn failures agree.
    """
    if isinstance(exc, (BranchSwitchError, worktrees.WorktreeError)):
        return "permanent"
    if isinstance(exc, AllAccountsBusy):
        return "all_busy"
    from server.sessions.failure import classify as _classify_turn
    kind = _classify_turn(None, str(exc)).kind
    if kind == "auth_401":
        return "auth"
    if kind == "rate_limit":
        return "quota"
    return "transient"
```

---

### Task 3: Re-enqueue + end helpers, attempt-cap state, account stash, gate

**Files:**
- Modify: `server/sessions/orchestrator.py`:
  - `__init__` (near `self._resume_recovery`): add `self._queue_start_attempts: dict[str, int] = {}`
  - module constant: `MAX_QUEUE_START_ATTEMPTS = 5`
  - `_spawn_session`'s `driver.start()` except block: stash the resolved account on the exception
  - `_maybe_start_queued`: extract the existing ended path into `_end_failed_start` (behavior-preserving)
  - new methods `_end_failed_start`, `_reenqueue_failed_start`, `_queue_entry_blocked`, `_account_needs_reauth`
  - `close_session` and `session_queue_cancel`: pop `self._queue_start_attempts` entry
- Test: `server/tests/test_session_queue_drain.py` (append; includes the `_enqueue_direct` helper Task 4 reuses)

**Interfaces:**
- Produces (Task 4 relies on these exact signatures):
  - `MAX_QUEUE_START_ATTEMPTS: int = 5` (module constant)
  - `self._queue_start_attempts: dict[str, int]`
  - `async def _end_failed_start(self, sid: str, params: dict) -> None` — today's ended path (re-seed ended record + DB UPDATE), extracted verbatim, plus a final `self._queue_start_attempts.pop(sid, None)`.
  - `async def _reenqueue_failed_start(self, sid: str, params: dict, record: SessionRecord) -> None` — teardown partial resources, restore `record` as `queued`, DB row back to `queued`, `sq.enqueue(sid, params)` at tail.
  - `def _queue_entry_blocked(self, params: dict) -> bool` — True when the entry's pinned account is quarantined or ledger-flagged `needs-reauth`.
  - Failed `driver.start()` exceptions carry `_ccswitch_account` (the resolved account name) via `setattr`.

Tests:

```python
def _enqueue_direct(orch, sid: str, *, account=None):
    """Insert a queued session deterministically (no cap interaction, no bg drain)."""
    from server.sessions.orchestrator import SYSTEM_IDENTITY
    return orch._enqueue_session(
        sid, name=sid, account=account, initial_prompt=None, cols=80, rows=24,
        cwd=None, branch=None, repo=None, identity=SYSTEM_IDENTITY,
        permission_mode="interactive", attachment_ids=None, name_is_auto=True,
        origin=None, origin_ref=None, model=None)


def test_reenqueue_failed_start_cleans_up_and_requeues(tmp_path):
    """_reenqueue_failed_start tears down worktree + X11 leftovers, restores the
    queued record, resets the DB row, and re-adds the entry at the queue tail."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'r1.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            from ccswitch import worktrees
            from server.sessions import session_queue as sq
            rec = await _enqueue_direct(orch, "sid-re", account="alpha")
            params = dict((await sq.next_entry()).params)
            # Simulate the drain loop's state at failure time: entry removed,
            # record popped, a partial worktree left behind by _spawn_session.
            await sq.remove("sid-re")
            orch._records.pop("sid-re")
            wt_dir = tmp_path / "leftover-wt"
            wt_dir.mkdir()
            orch._worktrees["sid-re"] = worktrees.WorktreeInfo(
                path=str(wt_dir), branch=None, is_child=False, repo=str(tmp_path))
            removed: list[str] = []
            with patch("ccswitch.worktrees.remove_worktree",
                       side_effect=lambda p: removed.append(p) or True):
                await orch._reenqueue_failed_start("sid-re", params, rec)
            assert removed == [str(wt_dir)]                      # worktree torn down
            assert "sid-re" not in orch._worktrees               # in-memory entry gone
            assert orch._records["sid-re"].status == "queued"    # record restored
            assert "sid-re" in (await sq.positions())            # back in the queue
            # DB row is (still) queued.
            from sqlalchemy import select as sa_select
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "sid-re"))).scalar_one()
                assert row.status == "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_queue_entry_blocked_gates_on_quarantine_and_reauth(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'r2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            # No pinned account → never gated (select_account handles eligibility).
            assert orch._queue_entry_blocked({"account": None}) is False
            assert orch._queue_entry_blocked({}) is False
            # Healthy pinned account → not gated.
            assert orch._queue_entry_blocked({"account": "alpha"}) is False
            # Quarantined pinned account → gated; expiry lifts the gate.
            orch._recovery.quarantine("alpha", until=orch._clock() + 60)
            assert orch._queue_entry_blocked({"account": "alpha"}) is True
            # lapse (quarantine() uses max() so can't lower an existing future entry)
            orch._recovery._quarantine["alpha"] = orch._clock() - 1
            assert orch._queue_entry_blocked({"account": "alpha"}) is False
            # needs-reauth in the ledger → gated (survives quarantine expiry).
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "needs-reauth")
            assert orch._queue_entry_blocked({"account": "alpha"}) is True
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "ok")
            assert orch._queue_entry_blocked({"account": "alpha"}) is False
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
```

Implementation pieces:

3a. Module constant:

```python
# A queued session whose start keeps failing for an UNCLASSIFIED transient reason
# is re-enqueued at most this many times before it is finally marked ended.
# Account-gated failures (auth/quota/all-busy) are never capped — they self-heal
# on re-auth and wait as ordinary queued sessions. In-memory: a daemon restart
# resets the count (a restart is a fresh chance).
MAX_QUEUE_START_ATTEMPTS = 5
```

3b. `__init__`, next to `self._resume_recovery: dict[str, int] = {}`:

```python
self._queue_start_attempts: dict[str, int] = {}
```

3c. `_spawn_session` `driver.start()` failure block becomes:

```python
        try:
            await driver.start()
        except Exception as exc:
            # driver.start() failed — remove the pre-seeded record so the session
            # does not appear as a ghost idle card in list_sessions. Stash the
            # resolved account on the exception so the queue drain loop can
            # quarantine it even for auto-picked (account=None) sessions.
            self._records.pop(session_id, None)
            setattr(exc, "_ccswitch_account", account)
            raise
```

3d. Extract today's ended path into `_end_failed_start` (placed right after `_maybe_start_queued`); the drain loop's `except` body becomes the log line + `await self._end_failed_start(sid, params)`:

```python
    async def _end_failed_start(self, sid: str, params: dict) -> None:
        """Terminal path for a failed queue start: mark the session ended.

        Re-seeds a minimal in-memory record if _spawn_session raised before
        creating one, then persists 'ended' to the DB row (which still exists
        as a 'queued' row — the UPDATE in _spawn_session was not reached).
        ended ∈ NON_ACTIVE_SESSION_STATUSES, so no cap slot is held.
        """
        if sid not in self._records:
            from datetime import datetime
            self._records[sid] = SessionRecord(
                id=sid,
                name=params.get("name", sid),
                account=params.get("account") or "",
                claude_session_id="",
                cols=params.get("cols", 220),
                rows=params.get("rows", 50),
                cwd=params.get("cwd") or "",
                repo=params.get("repo"),
                branch=None,
                status="ended",
                name_is_auto=params.get("name_is_auto", True),
                updated_at=datetime.utcnow(),
                origin=params.get("origin"),
                origin_ref=params.get("origin_ref"),
                model=params.get("model"),
            )
        else:
            self._records[sid].status = "ended"
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == sid)
                .values(status="ended")
            )
        self._queue_start_attempts.pop(sid, None)
```

3e. Re-enqueue helper:

```python
    async def _reenqueue_failed_start(self, sid: str, params: dict,
                                      record: SessionRecord) -> None:
        """Recoverable path for a failed queue start: clean up and stay queued.

        Tears down every partial resource _spawn_session may have created before
        it raised (worktree, X11 display, a driver registered late in the spawn),
        restores the in-memory record as 'queued', resets the DB row, and re-adds
        the entry at the queue TAIL so a repeatedly-failing head cannot starve
        the rest of the queue. Also repairs the pre-existing worktree/display
        leak of the old unconditional-ended path.
        """
        from server.sessions import session_queue as sq
        # Defensive: a failure after driver registration (e.g. the DB UPDATE)
        # would otherwise leak a live driver.
        driver = self._drivers.pop(sid, None)
        if driver is not None:
            with contextlib.suppress(Exception):
                await driver.aclose()
        self._pending_initial_prompt.pop(sid, None)
        self._pending_initial_attachment_ids.pop(sid, None)
        if self._x11 is not None:
            try:
                await self._x11.stop_display(sid)
            except Exception:
                log.exception("session_queue.reenqueue_display_cleanup_failed id=%s", sid)
        wt = self._worktrees.pop(sid, None)
        if wt is not None:
            try:
                await asyncio.to_thread(worktrees.remove_worktree, wt.path)
            except Exception:
                log.exception("session_queue.reenqueue_worktree_cleanup_failed id=%s path=%s",
                              sid, wt.path)
        record.status = "queued"
        self._records[sid] = record
        # The row normally still reads 'queued' (the UPDATE in _spawn_session was
        # not reached) — reset defensively in case the failure came later. Direct
        # UPDATE, not _persist_status: no status actually changed from the UI's
        # point of view, and _persist_status's broadcast/push side effects are
        # unwanted here.
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == sid)
                .values(status="queued")
            )
        await sq.enqueue(sid, params)
        log.info("session_queue.reenqueued id=%s", sid)
```

3f. Gate helpers:

```python
    def _account_needs_reauth(self, account: str) -> bool:
        """Best-effort ledger check; an unreadable ledger never blocks the queue."""
        try:
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                return ledger.get_status(led, account) == "needs-reauth"
        except Exception:
            return False

    def _queue_entry_blocked(self, params: dict) -> bool:
        """True when a queued entry's pinned account is known-unstartable.

        Cheap pre-spawn gate: skipping here avoids creating (and then tearing
        down) a worktree for a start we already know will fail. Auto-pick
        entries (no pinned account) are never gated — select_account excludes
        quarantined accounts itself and raises AllAccountsBusy.
        """
        account = params.get("account")
        if not account:
            return False
        if account in self._recovery.quarantined(now=self._clock()):
            return True
        return self._account_needs_reauth(account)
```

3g. In `close_session` (next to the other per-session dict pops) and in `session_queue_cancel` (after the `sq.remove` success check):

```python
        self._queue_start_attempts.pop(session_id, None)
```

---

### Task 4: Rewire the drain loop — classify, gate, re-enqueue

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_maybe_start_queued` body (including its docstring)
- Modify: `server/tests/test_session_queue_drain.py` — update `test_start_failure_ends_session_and_continues` (its `RuntimeError` now classifies as transient; switch to `BranchSwitchError` to keep guarding the permanent path) and append the behavior tests

**Interfaces:**
- Consumes: `sq.next_entry(exclude=...)` (Task 1), `_classify_start_failure(exc)` (Task 2), `_end_failed_start` / `_reenqueue_failed_start` / `_queue_entry_blocked` / `self._queue_start_attempts` / `MAX_QUEUE_START_ATTEMPTS` / exception attr `_ccswitch_account` (Task 3), `QUARANTINE_401` / `QUARANTINE_429`, `self._mark_needs_reauth`.

Existing-test update — in `test_start_failure_ends_session_and_continues`, sabotage with a PERMANENT failure:

```python
            from server.sessions.orchestrator import BranchSwitchError
            real = orch._spawn_session
            async def flaky(session_id, **kw):
                if session_id == bad.id:
                    raise BranchSwitchError("branch checked out elsewhere")
                return await real(session_id, **kw)
            orch._spawn_session = flaky
```

with docstring: `"""A PERMANENTLY failed-to-start session must be marked ended (not needs_attention).` / `Recoverable failures re-enqueue instead — covered by the tests below.` (rest of original docstring retained).

New behavior tests:

```python
def test_auth_failure_reenqueues_quarantines_and_marks_reauth(tmp_path):
    """Start-time 401 → session stays queued (re-added to the queue), the
    resolved account is quarantined + needs-reauth, and the same drain pass
    does not tight-loop (spawn attempted exactly once)."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a1.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            from server.sessions import session_queue as sq
            await _enqueue_direct(orch, "sid-auth", account="alpha")
            calls: list[str] = []
            async def fail_auth(session_id, **kw):
                calls.append(session_id)
                exc = RuntimeError("API error: 401 invalid authentication credentials")
                setattr(exc, "_ccswitch_account", "alpha")
                raise exc
            orch._spawn_session = fail_auth
            started = await orch._maybe_start_queued()
            assert started == 0
            assert calls == ["sid-auth"]                          # same-pass guard: one attempt
            assert orch._records["sid-auth"].status == "queued"   # NOT ended
            assert "sid-auth" in (await sq.positions())           # back in the queue
            assert "alpha" in orch._recovery.quarantined(now=orch._clock())
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                assert ledger.get_status(led, "alpha") == "needs-reauth"
            assert orch._queue_start_attempts == {}               # auth is never capped
            # Next drain: the quarantine gate skips it without calling spawn.
            started = await orch._maybe_start_queued()
            assert started == 0 and calls == ["sid-auth"]
            assert orch._records["sid-auth"].status == "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_gated_head_does_not_starve_next_entry(tmp_path):
    """A quarantined-account head is skipped IN PLACE (stays queued, keeps its
    position) while the next eligible entry starts into the free slot."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            from server.sessions import session_queue as sq
            _seed_account("beta")
            await _enqueue_direct(orch, "sid-gated", account="alpha")   # head
            await _enqueue_direct(orch, "sid-ok", account="beta")       # next
            orch._recovery.quarantine("alpha", until=orch._clock() + 60)
            started = await orch._maybe_start_queued()
            assert started == 1
            assert orch._records["sid-ok"].status != "queued"       # started
            assert orch._records["sid-gated"].status == "queued"    # skipped, kept
            assert (await sq.positions()) == {"sid-gated": 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_quarantine_expiry_self_heals(tmp_path):
    """Once the quarantine lapses (re-auth path clears it), the next drain
    starts the formerly-gated session normally."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a3.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            await _enqueue_direct(orch, "sid-heal", account="alpha")
            orch._recovery.quarantine("alpha", until=orch._clock() + 60)
            assert await orch._maybe_start_queued() == 0
            assert orch._records["sid-heal"].status == "queued"
            # lapse (quarantine() is max()/extend-only)
            orch._recovery._quarantine["alpha"] = orch._clock() - 1
            assert await orch._maybe_start_queued() == 1
            assert orch._records["sid-heal"].status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_all_accounts_busy_reenqueues_uncapped(tmp_path):
    """AllAccountsBusy (auto-pick, nothing eligible) → re-enqueue, no attempt
    counter (waits indefinitely like an ordinary queued session)."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a4.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            from server.sessions import session_queue as sq
            from server.sessions.selection import AllAccountsBusy
            await _enqueue_direct(orch, "sid-busy", account=None)
            async def fail_busy(session_id, **kw):
                raise AllAccountsBusy("no account has headroom")
            orch._spawn_session = fail_busy
            for _ in range(3):   # repeated drains never cap it out
                assert await orch._maybe_start_queued() == 0
            assert orch._records["sid-busy"].status == "queued"
            assert "sid-busy" in (await sq.positions())
            assert orch._queue_start_attempts == {}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_transient_failure_capped_then_ended(tmp_path):
    """An unclassified transient failure re-enqueues up to
    MAX_QUEUE_START_ATTEMPTS drains, then finally ends; counter is cleared."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a5.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            from server.sessions.orchestrator import MAX_QUEUE_START_ATTEMPTS
            from server.sessions import session_queue as sq
            await _enqueue_direct(orch, "sid-flaky", account="alpha")
            async def fail_transient(session_id, **kw):
                raise RuntimeError("boom")
            orch._spawn_session = fail_transient
            for i in range(MAX_QUEUE_START_ATTEMPTS - 1):
                assert await orch._maybe_start_queued() == 0
                assert orch._records["sid-flaky"].status == "queued", f"attempt {i + 1}"
                assert orch._queue_start_attempts["sid-flaky"] == i + 1
            assert await orch._maybe_start_queued() == 0          # final attempt
            assert orch._records["sid-flaky"].status == "ended"   # cap reached
            assert orch._queue_start_attempts == {}               # counter cleared
            assert "sid-flaky" not in (await sq.positions())
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_transient_success_clears_attempt_counter(tmp_path):
    """A successful start after transient failures resets the counter."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a6.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            await _enqueue_direct(orch, "sid-lucky", account="alpha")
            real = orch._spawn_session
            state = {"fail": 2}
            async def flaky(session_id, **kw):
                if state["fail"] > 0:
                    state["fail"] -= 1
                    raise RuntimeError("boom")
                return await real(session_id, **kw)
            orch._spawn_session = flaky
            assert await orch._maybe_start_queued() == 0
            assert await orch._maybe_start_queued() == 0
            assert orch._queue_start_attempts["sid-lucky"] == 2
            assert await orch._maybe_start_queued() == 1          # third time lucky
            assert orch._records["sid-lucky"].status != "queued"
            assert orch._queue_start_attempts == {}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
```

Drain-loop replacement (full body of `_maybe_start_queued`):

```python
    async def _maybe_start_queued(self) -> int:
        """Start queued sessions while capacity allows. Serialised by a lock so
        concurrent teardown paths can't double-start one entry. Returns count
        started.

        Start-failure policy (docs/superpowers/specs/
        2026-07-22-recoverable-queue-start-failures-design.md):

        - PERMANENT (bad branch / worktree) → ``ended``. A failed-to-start
          session holds no process; leaving it queued would retry a failure
          that cannot self-heal, and any active-ish status would consume a cap
          slot forever and starve the queue.
        - RECOVERABLE (auth 401 / quota 429 / no eligible account / transient)
          → cleaned up and RE-ENQUEUED at the tail, still ``queued``. Auth and
          quota failures quarantine the account (and 401s flag needs-reauth)
          so subsequent drains skip the entry cheaply until it can succeed.
          Unclassified transients are bounded by MAX_QUEUE_START_ATTEMPTS.

        A per-pass ``attempted`` set is passed to ``sq.next_entry(exclude=…)``
        so one pass never retries an entry it already attempted (a re-enqueued
        sole entry would otherwise tight-loop) and gated entries are skipped
        in place without losing their queue position.

        NEVER awaited inline inside a WS-attach coroutine — always scheduled via
        asyncio.create_task so a client-disconnect cancellation cannot interrupt it.
        """
        from server.sessions import session_queue as sq
        started = 0
        async with self._queue_drain_lock:
            attempted: set[str] = set()
            while True:
                if self._active_session_count() >= await self._max_active_sessions():
                    break
                entry = await sq.next_entry(exclude=attempted)
                if entry is None:
                    break
                sid, params = entry.session_id, dict(entry.params)
                attempted.add(sid)
                if self._queue_entry_blocked(params):
                    # Known-unstartable pinned account: leave the entry queued in
                    # place (no worktree churn); next_entry(exclude=) advances.
                    log.info("session_queue.start_gated id=%s account=%s",
                             sid, params.get("account"))
                    continue
                await sq.remove(sid)
                # Re-verify the DB row is still 'queued' before spawning.  A
                # concurrent close/archive (or crash-window orphan) may have already
                # changed the status while the entry sat in the queue.  Skipping here
                # heals those stale entries without double-starting the session.
                in_mem = self._records.get(sid)
                # A legitimately queued entry ALWAYS has an in-memory record
                # (enqueue and rehydrate both seed one) — a missing record means
                # a concurrent close/archive won the race; skip, never spawn.
                if in_mem is None or in_mem.status != "queued":
                    log.warning("session_queue.stale_entry id=%s status=%s — skipping",
                                sid, in_mem.status if in_mem is not None else "<gone>")
                    continue
                # Remove the queued pre-record so it doesn't double-count while
                # _spawn_session re-inserts it as an active record.
                self._records.pop(sid, None)
                try:
                    await self._spawn_session(sid, preexisting_row=True, **params)
                    started += 1
                    self._queue_start_attempts.pop(sid, None)
                except Exception as exc:
                    kind = _classify_start_failure(exc)
                    account = getattr(exc, "_ccswitch_account", None) or params.get("account")
                    log.warning("session_queue.start_failed id=%s kind=%s account=%s",
                                sid, kind, account, exc_info=True)
                    if kind == "permanent":
                        await self._end_failed_start(sid, params)
                    elif kind in ("auth", "quota"):
                        # Quarantine so the FIRST failure trips the pre-spawn gate
                        # and later drains skip without churn until re-auth/reset.
                        if account:
                            now = self._clock()
                            cooldown = QUARANTINE_401 if kind == "auth" else QUARANTINE_429
                            self._recovery.quarantine(account, until=now + cooldown)
                            if kind == "auth":
                                self._mark_needs_reauth(account)
                        await self._reenqueue_failed_start(sid, params, in_mem)
                    elif kind == "all_busy":
                        await self._reenqueue_failed_start(sid, params, in_mem)
                    else:  # unclassified transient — recoverable but capped
                        n = self._queue_start_attempts.get(sid, 0) + 1
                        if n >= MAX_QUEUE_START_ATTEMPTS:
                            await self._end_failed_start(sid, params)
                        else:
                            self._queue_start_attempts[sid] = n
                            await self._reenqueue_failed_start(sid, params, in_mem)
        return started
```

(`_end_failed_start` already pops `_queue_start_attempts`, so the cap branch needs no explicit pop.)

---

### Task 5: Full regression + gates

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -q --ignore=server/tests/daemon_properties --deselect server/tests/test_eta_parity_live.py 2>&1 | tail -15`, then `python3 -m ruff check server/ src/` and `python3 -m mypy server/ --ignore-missing-imports`. Known environment-dependent failures (not regressions): `test_copilot_proxy_resolve` (binary installed locally), `eta_parity_live` (real data — deselected).

---

## Self-Review (completed at plan time)

- **Spec coverage:** §1 classification → Task 2; §2 gating (both halves) → Tasks 3/4; §3 clean re-enqueue → Task 3; §4 same-pass guard + attempt cap → Tasks 1/3/4; latent worktree/X11 leak → Task 3; every Testing bullet maps to a named test; regression → Task 5.
- **Deviation from spec pseudocode (intentional):** the spec's `if sid in attempted: break` same-pass guard is replaced by `next_entry(exclude=attempted)` — the spec's §2 "skip and advance" is impossible with a head-only `next_entry`, and the exclusion set implements both behaviors with one mechanism.
- **Type consistency:** classifier returns `"permanent"|"auth"|"quota"|"all_busy"|"transient"` and Task 4 dispatches on exactly those; `_reenqueue_failed_start(sid, params, record)` called with `in_mem` in all three recoverable branches.
