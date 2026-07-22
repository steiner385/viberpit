# Max Active Sessions + Priority Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A configurable global cap on active sessions (default 5); overflow sessions from ANY origin become `status="queued"` rows that drain in priority order, reorderable from a sidebar queue UI.

**Architecture:** Queued sessions are real Session rows plus a `session_queue` table (priority + deferred spawn params). `create_session` becomes gate → (`_spawn_session` | enqueue). A drain hook (`_maybe_start_queued`) fires on every active-set exit, on cap raise, and after rehydrate. Pure queue CRUD lives in a new `server/sessions/session_queue.py`; the orchestrator only orchestrates. Spec: `docs/superpowers/specs/2026-07-21-max-active-sessions-queue-design.md`.

**Tech Stack:** Python 3.12 / SQLAlchemy async (aiosqlite) / FastAPI · React+TS, React Query, vitest.

## Global Constraints

- Default cap **5**, min **1**. `AppSettings.max_active_sessions` nullable Integer; NULL means "use default 5" (nullable so `init_db` backfills without a migration — dev SQLite never ALTERs).
- Active (slot-consuming) = `status not in NON_ACTIVE_SESSION_STATUSES` where `NON_ACTIVE_SESSION_STATUSES = INACTIVE_SESSION_STATUSES | {"queued"}` — defined ONCE in `server/db/models.py`; no inline status math anywhere.
- `INACTIVE_SESSION_STATUSES` stays exactly `frozenset({"ended", "archived"})`.
- Naming: session-level queue methods are `session_queue_*` — the name `cancel_queued`/`queued` is TAKEN by the per-driver PROMPT queue (see `server/ipc/orchestrator_rpc.py:266-267`); never reuse those names.
- Every new orchestrator method the API calls needs a `RemoteOrchestrator` async proxy (`server/sessions/remote.py`) AND an entry in the `_SIMPLE` RPC list (`server/ipc/orchestrator_rpc.py:266-278`) — breadth tests fail otherwise.
- Lowering the cap never stops running sessions; unarchive/resume bypass the cap (v1).
- Worktree + account selection happen at DEQUEUE (in `_spawn_session`), never at enqueue.
- Tests: server → `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/... ` (add `--ignore=server/tests/test_daemon_properties.py` for multi-file runs); web → `cd web && npx vitest run <file>`; gates `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `cd web && npm run build && npx eslint src`.
- Known pre-existing local failures (NOT regressions — do not chase): `test_archive.py` (6, environmental), `test_copilot_proxy_resolve.py`, `test_eta_parity_live.py`.
- UI: queue controls go in the card's actions group, NEVER inside the row's open-button (nested-interactive a11y). No new dashboard tab.
- Commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: DB model, status SoT, and settings column

**Files:**
- Modify: `server/db/models.py` (after `INACTIVE_SESSION_STATUSES` at line 33; new table after `AppSettings` ~line 285; new column inside `AppSettings`)
- Test: create `server/tests/test_session_queue_model.py`

**Interfaces:**
- Produces: `NON_ACTIVE_SESSION_STATUSES: frozenset[str]`; model `SessionQueue` (`__tablename__="session_queue"`, cols `session_id: str PK`, `priority: int`, `enqueued_at: datetime tz`, `params: dict` JSON); `AppSettings.max_active_sessions: int | None`; constant `DEFAULT_MAX_ACTIVE_SESSIONS = 5` in `server/db/models.py`.

- [ ] **Step 1: Write the failing test**

```python
"""session_queue table + status SoT + max_active_sessions setting."""
import asyncio


def test_non_active_statuses_superset():
    from server.db.models import (DEFAULT_MAX_ACTIVE_SESSIONS,
                                  INACTIVE_SESSION_STATUSES,
                                  NON_ACTIVE_SESSION_STATUSES)
    assert INACTIVE_SESSION_STATUSES == frozenset({"ended", "archived"})
    assert NON_ACTIVE_SESSION_STATUSES == INACTIVE_SESSION_STATUSES | {"queued"}
    assert DEFAULT_MAX_ACTIVE_SESSIONS == 5


def test_session_queue_roundtrip(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
        from datetime import datetime, timezone
        from sqlalchemy import select
        from server.db.models import SessionQueue
        async with db_session() as db:
            db.add(SessionQueue(session_id="s1", priority=1,
                                enqueued_at=datetime.now(timezone.utc),
                                params={"initial_prompt": "hi", "cols": 80}))
        async with db_session() as db:
            row = (await db.execute(select(SessionQueue))).scalar_one()
            assert row.session_id == "s1"
            assert row.params["initial_prompt"] == "hi"

    asyncio.run(run())


def test_app_settings_max_active_nullable(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        from server.db.models import AppSettings
        async with db_session() as db:
            db.add(AppSettings(id="singleton"))
        from sqlalchemy import select
        async with db_session() as db:
            s = (await db.execute(select(AppSettings))).scalar_one()
            assert s.max_active_sessions is None  # NULL → default 5 applies

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_session_queue_model.py -v`
Expected: ImportError (`NON_ACTIVE_SESSION_STATUSES` / `SessionQueue` undefined).

- [ ] **Step 3: Implement in `server/db/models.py`**

Directly under `INACTIVE_SESSION_STATUSES` (line 33):

```python
# Statuses that do NOT consume an active-session slot. "queued" rows are real
# sessions waiting for capacity — neither reviewable (inactive) nor running.
# EVERY cap/slot count derives from this set; no inline status math elsewhere.
NON_ACTIVE_SESSION_STATUSES: frozenset[str] = INACTIVE_SESSION_STATUSES | {"queued"}

# Global cap on concurrently active sessions when AppSettings.max_active_sessions
# is NULL. See docs/superpowers/specs/2026-07-21-max-active-sessions-queue-design.md.
DEFAULT_MAX_ACTIVE_SESSIONS = 5
```

Inside `AppSettings`, after `archive_home` (~line 284):

```python
    # Global cap on concurrently active sessions; overflow spawns queue.
    # NULL = DEFAULT_MAX_ACTIVE_SESSIONS. Nullable so init_db backfills it
    # without a migration.
    max_active_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```

New model after `AppSettings` (match the file's mapped_column style; `JSON` is already imported for other models — verify, else add to the sqlalchemy import line):

```python
class SessionQueue(Base):
    """Ordering + deferred spawn parameters for status='queued' sessions.

    One row per queued session; deleted when the session starts, is cancelled,
    or fails to start. `params` holds the create_session kwargs that the
    sessions row does not (cols, rows, attachment_ids, identity, requested
    branch/cwd/account) — worktree and account rotation are decided at DEQUEUE.
    """

    __tablename__ = "session_queue"

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.id"), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
```

(`ForeignKey`, `DateTime`, `_utcnow` are already used elsewhere in the file — reuse the same imports/helpers; add any missing name to the existing import lines rather than new import blocks.)

- [ ] **Step 4: Run test to verify it passes**

Run: same command. Expected: 3 passed. Also run `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_cicd_models.py -q` (nearest neighbor) to confirm no model-layer breakage.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_session_queue_model.py
git commit -m "feat(db): session_queue table, queued status SoT, max_active_sessions setting

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Pure queue helpers — `server/sessions/session_queue.py`

**Files:**
- Create: `server/sessions/session_queue.py`
- Test: create `server/tests/test_session_queue_helpers.py`

**Interfaces:**
- Consumes: `SessionQueue` from Task 1.
- Produces (all async, each opens its own `db_session()`):
  - `enqueue(session_id: str, params: dict) -> int` — appends at back (max priority + 1, starting at 1); returns assigned priority.
  - `next_entry() -> SessionQueue | None` — head by `(priority, enqueued_at)`.
  - `remove(session_id: str) -> bool` — delete row, True if existed.
  - `move(session_id: str, op: str, position: int | None = None) -> bool` — `op ∈ {"up","down","top","position"}`; renumbers ALL rows 1..N in one transaction (queue is small); returns False for unknown id.
  - `positions() -> dict[str, int]` — session_id → 1-based position ordered by `(priority, enqueued_at)`.

- [ ] **Step 1: Write the failing test**

```python
"""Pure session-queue CRUD: ordering, moves, renumbering."""
import asyncio


async def _seed(n):
    from server.sessions import session_queue as sq
    for i in range(1, n + 1):
        await sq.enqueue(f"s{i}", {"n": i})


def _run(tmp_path, coro_fn):
    from server.db import init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
        await coro_fn()

    asyncio.run(run())


def test_enqueue_appends_and_next_is_fifo(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        p1 = await sq.enqueue("s1", {})
        p2 = await sq.enqueue("s2", {})
        assert (p1, p2) == (1, 2)
        head = await sq.next_entry()
        assert head.session_id == "s1"
    _run(tmp_path, body)


def test_move_up_down_top_and_positions(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(4)                                   # s1 s2 s3 s4
        assert await sq.move("s3", "up")                 # s1 s3 s2 s4
        assert await sq.move("s4", "top")                # s4 s1 s3 s2
        assert await sq.move("s1", "down")               # s4 s3 s1 s2
        assert await sq.move("s2", "position", 2)        # s4 s2 s3 s1
        assert (await sq.positions()) == {"s4": 1, "s2": 2, "s3": 3, "s1": 4}
    _run(tmp_path, body)


def test_move_bounds_are_noops(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(2)
        assert await sq.move("s1", "up")        # already head → True, unchanged
        assert (await sq.positions())["s1"] == 1
        assert not await sq.move("ghost", "up")  # unknown id → False
    _run(tmp_path, body)


def test_remove(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(2)
        assert await sq.remove("s1")
        assert not await sq.remove("s1")
        assert (await sq.positions()) == {"s2": 1}
    _run(tmp_path, body)
```

- [ ] **Step 2: Run to verify failure** — `... pytest server/tests/test_session_queue_helpers.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement `server/sessions/session_queue.py`**

```python
"""Pure CRUD for the session queue (status='queued' sessions).

Ordering is (priority ASC, enqueued_at ASC). Mutations renumber priorities to
1..N in ONE transaction — the queue is human-scale, clarity beats cleverness.
No orchestrator imports: the orchestrator calls this module, never vice versa.
"""
from __future__ import annotations

from sqlalchemy import delete as sa_delete, select

from server.db import db_session
from server.db.models import SessionQueue

_ORDER = (SessionQueue.priority, SessionQueue.enqueued_at)


async def enqueue(session_id: str, params: dict) -> int:
    async with db_session() as db:
        rows = (await db.execute(select(SessionQueue.priority))).scalars().all()
        priority = (max(rows) + 1) if rows else 1
        db.add(SessionQueue(session_id=session_id, priority=priority, params=params))
        return priority


async def next_entry() -> SessionQueue | None:
    async with db_session() as db:
        return (await db.execute(
            select(SessionQueue).order_by(*_ORDER).limit(1))).scalar_one_or_none()


async def remove(session_id: str) -> bool:
    async with db_session() as db:
        r = await db.execute(
            sa_delete(SessionQueue).where(SessionQueue.session_id == session_id))
        return bool(r.rowcount)


async def positions() -> dict[str, int]:
    async with db_session() as db:
        ids = (await db.execute(
            select(SessionQueue.session_id).order_by(*_ORDER))).scalars().all()
    return {sid: i + 1 for i, sid in enumerate(ids)}


async def move(session_id: str, op: str, position: int | None = None) -> bool:
    """Reorder one entry. op: up | down | top | position (1-based `position`)."""
    async with db_session() as db:
        rows = (await db.execute(
            select(SessionQueue).order_by(*_ORDER))).scalars().all()
        ids = [r.session_id for r in rows]
        if session_id not in ids:
            return False
        idx = ids.index(session_id)
        if op == "up":
            new = max(0, idx - 1)
        elif op == "down":
            new = min(len(ids) - 1, idx + 1)
        elif op == "top":
            new = 0
        elif op == "position" and position is not None:
            new = min(max(0, position - 1), len(ids) - 1)
        else:
            return False
        ids.insert(new, ids.pop(idx))
        by_id = {r.session_id: r for r in rows}
        for i, sid in enumerate(ids):
            by_id[sid].priority = i + 1
        return True
```

- [ ] **Step 4: Run to verify pass** — same command → 4 passed.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/session_queue.py server/tests/test_session_queue_helpers.py
git commit -m "feat(sessions): pure session-queue CRUD helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Orchestrator — cap gate + enqueue in `create_session`

**Files:**
- Modify: `server/sessions/orchestrator.py` (`create_session` at ~line 702; new helpers beside it)
- Test: create `server/tests/test_session_cap_enqueue.py`

**Interfaces:**
- Consumes: Task 1 constants, Task 2 helpers.
- Produces:
  - `Orchestrator._active_session_count() -> int` — `sum(1 for r in self._records.values() if r.status not in NON_ACTIVE_SESSION_STATUSES)`.
  - `Orchestrator._max_active_sessions() -> int` (async) — reads `AppSettings.max_active_sessions`, NULL/missing row → `DEFAULT_MAX_ACTIVE_SESSIONS`, floor 1.
  - `create_session(...)` unchanged signature; may return a `SessionRecord` with `status="queued"`.
  - Internal split: the existing body of `create_session` (everything from `repo_root = ...` through `return record`) moves verbatim into `async def _spawn_session(self, session_id, *, name, account, initial_prompt, cols, rows, cwd, branch, repo, identity, permission_mode, attachment_ids, name_is_auto, origin, origin_ref, model, preexisting_row: bool = False) -> SessionRecord` — with two mechanical adaptations: (a) `session_id` is a parameter, not generated inside; (b) when `preexisting_row=True` the DB-persist block UPDATEs the existing row (status→"idle", account_name, cwd, branch, model) instead of `db.add`-ing a new one. Task 4 relies on `_spawn_session` with `preexisting_row=True`.

- [ ] **Step 1: Write the failing test**

Use the orchestrator test conventions already in `server/tests/` (find the lightest existing orchestrator construction — `grep -l "Orchestrator(" server/tests` and copy the minimal fixture; several tests build it with a fake session factory). The behavioral contract:

```python
"""Cap gate: sessions beyond max_active_sessions enqueue instead of spawning."""
import asyncio


def test_sixth_session_queues_and_automations_queue_too(tmp_path, orch_factory):
    """With max=2: 2 spawn live, the 3rd (user) and 4th (automation origin)
    both become status='queued' rows with session_queue entries, and no
    driver/worktree/account work runs for them."""

    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}",
                                  max_active_sessions=2)
        a = await orch.create_session("one")
        b = await orch.create_session("two")
        assert {a.status, b.status} == {"idle"}
        c = await orch.create_session("three", initial_prompt="hi")
        d = await orch.create_session("four", origin="backlog-runner",
                                      origin_ref="r#1")
        assert c.status == "queued" and d.status == "queued"
        from server.sessions import session_queue as sq
        pos = await sq.positions()
        assert pos == {c.id: 1, d.id: 2}
        entry = await sq.next_entry()
        assert entry.params["initial_prompt"] == "hi"
        # No driver was started for queued sessions:
        assert c.id not in orch._drivers and d.id not in orch._drivers

    asyncio.run(run())


def test_cap_counts_only_active_statuses(tmp_path, orch_factory):
    """ended/archived/queued records do not consume slots."""

    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'c2.db'}",
                                  max_active_sessions=1)
        a = await orch.create_session("one")
        await orch.close_session(a.id)          # frees the slot
        b = await orch.create_session("two")
        assert b.status == "idle"               # spawned, not queued

    asyncio.run(run())
```

Write the `orch_factory` fixture in this file by copying the smallest existing orchestrator fixture (fake SDK/session factory, no real claude), extended to write `AppSettings(id="singleton", max_active_sessions=N)` after `init_db`. If per-fixture wiring is genuinely heavier than expected, report NEEDS_CONTEXT naming the closest existing fixture rather than inventing a parallel harness.

- [ ] **Step 2: Run to verify failure** — 3rd session spawns today → `c.status == "queued"` fails.

- [ ] **Step 3: Implement**

In `create_session`, after the cwd-validation block and `session_id = str(uuid.uuid4())` (~line 732), insert the gate; everything below it moves to `_spawn_session`:

```python
        # Global active-session cap: at/over the cap the session is created as a
        # queued row (real session, no worktree/account/driver yet) and started
        # later by _maybe_start_queued in (priority, enqueued_at) order. Applies
        # to EVERY origin — automations included (their claims simply wait).
        if self._active_session_count() >= await self._max_active_sessions():
            return await self._enqueue_session(
                session_id, name=name, account=account,
                initial_prompt=initial_prompt, cols=cols, rows=rows, cwd=cwd,
                branch=branch, repo=repo, identity=identity,
                permission_mode=permission_mode, attachment_ids=attachment_ids,
                name_is_auto=name_is_auto, origin=origin, origin_ref=origin_ref,
                model=model)
        return await self._spawn_session(
            session_id, name=name, account=account, initial_prompt=initial_prompt,
            cols=cols, rows=rows, cwd=cwd, branch=branch, repo=repo,
            identity=identity, permission_mode=permission_mode,
            attachment_ids=attachment_ids, name_is_auto=name_is_auto,
            origin=origin, origin_ref=origin_ref, model=model)
```

New helpers (place directly after `create_session`):

```python
    def _active_session_count(self) -> int:
        return sum(1 for r in self._records.values()
                   if r.status not in NON_ACTIVE_SESSION_STATUSES)

    async def _max_active_sessions(self) -> int:
        from sqlalchemy import select as sa_select
        from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS, AppSettings
        async with db_session() as db:
            s = (await db.execute(sa_select(AppSettings))).scalar_one_or_none()
        val = getattr(s, "max_active_sessions", None) if s else None
        return max(1, val if val is not None else DEFAULT_MAX_ACTIVE_SESSIONS)

    async def _enqueue_session(self, session_id: str, *, name, account,
                               initial_prompt, cols, rows, cwd, branch, repo,
                               identity, permission_mode, attachment_ids,
                               name_is_auto, origin, origin_ref, model) -> SessionRecord:
        from datetime import datetime
        from server.sessions import session_queue as sq
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=name, account_name=account,
                claude_session_id=None, status="queued", pid=None,
                cwd=cwd, repo=repo, branch=None,
                permission_mode=permission_mode, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref, model=model,
                initial_prompt=initial_prompt))
        priority = await sq.enqueue(session_id, {
            "name": name, "account": account, "initial_prompt": initial_prompt,
            "cols": cols, "rows": rows, "cwd": cwd, "branch": branch,
            "repo": repo, "identity": identity,
            "permission_mode": permission_mode,
            "attachment_ids": attachment_ids, "name_is_auto": name_is_auto,
            "origin": origin, "origin_ref": origin_ref, "model": model,
        })
        rec = SessionRecord(
            id=session_id, name=name, account=account or "", claude_session_id="",
            cols=cols, rows=rows, cwd=cwd or "", repo=repo, branch=None,
            status="queued", name_is_auto=name_is_auto,
            updated_at=datetime.utcnow(), origin=origin, origin_ref=origin_ref,
            model=model)
        self._records[session_id] = rec
        log.info("session.queued id=%s priority=%d origin=%s",
                 session_id, priority, origin)
        return rec
```

(`NON_ACTIVE_SESSION_STATUSES` joins the existing models import at line 42. If `SessionRecord` requires fields not listed here, fill them the way `_pre_record` at ~line 803 does.)

Then perform the mechanical `_spawn_session` split described in **Interfaces** (move, don't rewrite; keep every comment).

- [ ] **Step 4: Run to verify pass** — new file passes; then regression: `... pytest server/tests/ -k "orchestrator or session" -q --ignore=server/tests/test_daemon_properties.py` — pre-existing tests must stay green (they run below the cap because each test spawns few sessions; any that spawn ≥5 need their fixture to set `max_active_sessions` high — fix by seeding AppSettings in that test's DB, never by weakening the gate).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_session_cap_enqueue.py
git commit -m "feat(sessions): global active-session cap — overflow enqueues as status=queued

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Orchestrator — drain (`_maybe_start_queued`) + start/failure paths

**Files:**
- Modify: `server/sessions/orchestrator.py` (hooks in `close_session` ~line 892, `archive_session` ~line 5391, `_persist_status` ~line 980, `rehydrate` ~line 3063)
- Test: create `server/tests/test_session_queue_drain.py`

**Interfaces:**
- Consumes: Task 2 helpers, Task 3's `_spawn_session(..., preexisting_row=True)`.
- Produces: `Orchestrator._maybe_start_queued() -> int` (returns sessions started); called with `asyncio.create_task` from teardown paths (never awaited inline on a WS-attach coroutine); Task 5/6 rely on drain running after cancel/cap-raise.

- [ ] **Step 1: Write the failing tests**

```python
"""Queue drain: slot-free → head starts; failure path; restart rehydrate."""
import asyncio


def test_close_drains_head_in_priority_order(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}",
                                  max_active_sessions=1)
        a = await orch.create_session("one")
        b = await orch.create_session("two")     # queued pos 1
        c = await orch.create_session("three")   # queued pos 2
        from server.sessions import session_queue as sq
        await sq.move(c.id, "top")               # c overtakes b
        await orch.close_session(a.id)
        await orch._maybe_start_queued()         # deterministic in tests
        assert orch._records[c.id].status != "queued"   # c started
        assert orch._records[b.id].status == "queued"   # b still waits
        assert (await sq.positions()) == {b.id: 1}

    asyncio.run(run())


def test_start_failure_marks_needs_attention_and_continues(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'d2.db'}",
                                  max_active_sessions=1)
        a = await orch.create_session("one")
        bad = await orch.create_session("bad")
        ok = await orch.create_session("ok")
        # Sabotage the head's start (monkeypatch _spawn_session to raise once
        # for bad.id — e.g. wrap the real method).
        real = orch._spawn_session
        async def flaky(session_id, **kw):
            if session_id == bad.id:
                raise RuntimeError("insufficient disk headroom at /x")
            return await real(session_id, **kw)
        orch._spawn_session = flaky
        await orch.close_session(a.id)
        started = await orch._maybe_start_queued()
        assert started == 1                                  # ok started
        assert orch._records[bad.id].status == "needs_attention"
        from server.sessions import session_queue as sq
        assert (await sq.positions()) == {}                  # both rows gone

    asyncio.run(run())


def test_rehydrate_preserves_queued_and_drains(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'d3.db'}",
                                  max_active_sessions=1)
        a = await orch.create_session("one")
        b = await orch.create_session("two")            # queued
        await orch.close_session(a.id)
        # Simulate daemon restart: fresh orchestrator over the same DB.
        orch2 = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'d3.db'}",
                                   max_active_sessions=1, reuse_db=True)
        await orch2.rehydrate()
        assert orch2._records[b.id].status == "queued"  # survived as queued
        await orch2._maybe_start_queued()
        assert orch2._records[b.id].status != "queued"  # drained into the free slot

    asyncio.run(run())
```

- [ ] **Step 2: Run to verify failure** — `_maybe_start_queued` doesn't exist.

- [ ] **Step 3: Implement**

New method (near `_stuck_session_sweep` ~line 4946 or after `_enqueue_session`):

```python
    async def _maybe_start_queued(self) -> int:
        """Start queued sessions while capacity allows. Serialized by a lock so
        concurrent teardown paths can't double-start one entry. Returns count
        started. Start failure → row leaves the queue, session goes
        needs_attention, the NEXT entry gets its chance (one bad head must not
        block the queue)."""
        from server.sessions import session_queue as sq
        started = 0
        async with self._queue_drain_lock:
            while True:
                if self._active_session_count() >= await self._max_active_sessions():
                    break
                entry = await sq.next_entry()
                if entry is None:
                    break
                sid, params = entry.session_id, dict(entry.params)
                await sq.remove(sid)
                # The queued pre-record must not double-count while starting.
                self._records.pop(sid, None)
                try:
                    await self._spawn_session(sid, preexisting_row=True, **params)
                    started += 1
                except Exception:
                    log.warning("session_queue.start_failed id=%s", sid, exc_info=True)
                    await self._persist_status(sid, "needs_attention")
                    rec = self._records.get(sid)
                    if rec is not None:
                        rec.status = "needs_attention"
        return started
```

Add `self._queue_drain_lock = asyncio.Lock()` in `__init__` beside the other locks. On the failure path: if `_spawn_session` failed before re-seeding `_records`, rebuild a detached record from the DB row the way `rehydrate` does (or re-add the popped record with status `needs_attention`) so the session stays visible — mirror whichever the existing failure handling in `create_session` (record-pop at ~line 831) makes simplest, but the session MUST remain listed with `needs_attention`.

Hooks — fire-and-forget at the END of each teardown path (after the status is persisted):

- `close_session` (~line 892, after the DB row is marked ended): `asyncio.create_task(self._maybe_start_queued())`
- `archive_session` (~line 5391, same placement)
- `_persist_status` (~line 980): when the NEW status lands in `INACTIVE_SESSION_STATUSES`, schedule the same task (covers driver self-exit without an explicit close).
- `rehydrate` (~line 3063): queued rows are selected by the existing `notin_(INACTIVE_SESSION_STATUSES)` query already — verify the mid-turn normalization does NOT touch `"queued"` (it only rewrites `working/in_progress/condensing`), then schedule a drain task after the rehydrate loop completes.

- [ ] **Step 4: Run to verify pass** — new file green, then `... pytest server/tests/ -k "queue or orchestrator" -q --ignore=server/tests/test_daemon_properties.py`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_session_queue_drain.py
git commit -m "feat(sessions): queue drain on slot-free/rehydrate with failure isolation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Orchestrator queue ops + settings write-path drain

**Files:**
- Modify: `server/sessions/orchestrator.py`; `server/api/settings.py` (~line 121 pattern)
- Test: create `server/tests/test_session_queue_ops.py`; extend the existing settings test file (locate via `grep -l maxArchivedSessions server/tests`)

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces (called by Task 6's API; ALL must be JSON-safe returns):
  - `session_queue_move(session_id: str, op: str, position: int | None = None) -> bool` — delegates to `sq.move`, then (only for a successful op) returns True.
  - `session_queue_cancel(session_id: str) -> bool` — `sq.remove`; on success mark row + record `ended`, emit nothing session-side beyond the status persist, return True.
  - `session_queue_positions() -> dict[str, int]` — passthrough of `sq.positions()`.
  - Settings PUT accepts `maxActiveSessions: int | None` (clamped `>= 1`); `_to_dict` returns it; after a save that RAISES the cap, the API triggers the daemon drain via the orchestrator (`request.app.state` orchestrator handle, same access pattern the sessions API uses) — fire-and-forget.

- [ ] **Step 1: Write the failing tests**

```python
"""session_queue_move/cancel/positions on the orchestrator."""
import asyncio


def test_move_and_positions(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'o.db'}",
                                  max_active_sessions=1)
        await orch.create_session("one")
        b = await orch.create_session("b")
        c = await orch.create_session("c")
        assert await orch.session_queue_move(c.id, "top")
        assert await orch.session_queue_positions() == {c.id: 1, b.id: 2}
        assert not await orch.session_queue_move("ghost", "up")

    asyncio.run(run())


def test_cancel_marks_ended(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path/'o2.db'}",
                                  max_active_sessions=1)
        await orch.create_session("one")
        b = await orch.create_session("b")
        assert await orch.session_queue_cancel(b.id)
        assert orch._records[b.id].status == "ended"
        assert await orch.session_queue_positions() == {}
        assert not await orch.session_queue_cancel(b.id)   # already gone

    asyncio.run(run())
```

Settings test additions (in the existing settings test file, copying its client fixture): PUT `{"maxActiveSessions": 3}` → GET returns 3; PUT `{"maxActiveSessions": 0}` → stored value is 1 (clamped); default GET returns None (UI shows placeholder "5 (default)").

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

Orchestrator methods (beside `_maybe_start_queued`):

```python
    async def session_queue_move(self, session_id: str, op: str,
                                 position: int | None = None) -> bool:
        from server.sessions import session_queue as sq
        return await sq.move(session_id, op, position)

    async def session_queue_positions(self) -> dict[str, int]:
        from server.sessions import session_queue as sq
        return await sq.positions()

    async def session_queue_cancel(self, session_id: str) -> bool:
        from server.sessions import session_queue as sq
        if not await sq.remove(session_id):
            return False
        await self._persist_status(session_id, "ended")
        rec = self._records.get(session_id)
        if rec is not None:
            rec.status = "ended"
        return True
```

`server/api/settings.py`: add `maxActiveSessions: int | None = None` to `SettingsBody` (line 34 area); in the PUT handler mirror the `maxArchivedSessions` block (line 121-124) with `row.max_active_sessions = max(1, body.maxActiveSessions)`; add to `_to_dict` (line 100 area): `"maxActiveSessions": s.max_active_sessions`. After commit, if the value increased, schedule the drain through the same orchestrator handle the sessions API uses (read `server/api/sessions.py` for the accessor — one line: `asyncio.create_task(orch._maybe_start_queued())`; if only a Remote proxy is reachable from the edge process, add `maybe_start_queued` to the Task 6 RPC set instead and call that).

- [ ] **Step 4: Run to verify pass** — both test files + the settings file.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/api/settings.py server/tests/test_session_queue_ops.py server/tests/<settings-test-file>
git commit -m "feat(sessions): queue move/cancel/positions ops; maxActiveSessions setting

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: API endpoints, RPC proxies, queuePosition in session payloads

**Files:**
- Modify: `server/api/sessions.py` (list/detail serialization + new routes); `server/sessions/remote.py` (~line 270 class); `server/ipc/orchestrator_rpc.py` (`_SIMPLE` list ~line 266)
- Test: create `server/tests/test_session_queue_api.py`

**Interfaces:**
- Consumes: Task 5 methods.
- Produces:
  - `POST /api/sessions/{id}/queue-move` body `{"op": "up"|"down"|"top"|"position", "position"?: int}` → 200 `{"ok": true}` | 404 when not queued.
  - `DELETE /api/sessions/{id}/queue` → 204 | 404.
  - Session payloads gain `queuePosition: int | null` (null when not queued) — computed once per list call from `session_queue_positions()`.
  - `RemoteOrchestrator.session_queue_move/session_queue_cancel/session_queue_positions/maybe_start_queued` async proxies; `_SIMPLE` grows `"session_queue_move", "session_queue_cancel", "session_queue_positions", "maybe_start_queued"` (add a public `async def maybe_start_queued(self) -> int: return await self._maybe_start_queued()` wrapper on the orchestrator — RPC names must not start with `_`).

- [ ] **Step 1: Write the failing tests** — copy the app/client fixture idiom from an existing `server/tests/test_sessions_api*.py` file (in-process orchestrator adapter). Assert: queued session's list payload has `queuePosition: 1`; live session has `queuePosition: None`; `queue-move` with `{"op":"top"}` flips positions; move on a non-queued id → 404; DELETE → 204 and the session's status becomes `ended`; second DELETE → 404. Remember the `:memory:` seed-visibility flake — use a temp-file `CCSWITCH_DB_URL`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — routes beside the existing session action routes in `server/api/sessions.py` (copy the 404/permission shape of the closest action endpoint, e.g. the autopilot toggle); serialization: fetch `positions = await orch.session_queue_positions()` once in the list handler and stamp `queuePosition=positions.get(s.id)`; detail handler same. RemoteOrchestrator proxies follow the `prs_for` pattern (line 288-291): `r = await self._client.call("session_queue_move", session_id=..., op=..., position=...); return r` — `_SIMPLE` handlers pass params through as kwargs, so keep parameter names identical to the orchestrator methods.

- [ ] **Step 4: Run to verify pass** — plus the RPC/proxy breadth test family: `... pytest server/tests/ -k "remote or rpc or breadth" -q --ignore=server/tests/test_daemon_properties.py`.

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_session_queue_api.py
git commit -m "feat(api): queue-move/cancel endpoints, queuePosition, RPC proxies

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Web — types, hooks, queued sidebar section

**Files:**
- Modify: `web/src/api/types.ts` (Session type), `web/src/api/hooks.ts` (AppSettings type ~line 166-176; new mutations after `useSetAutopilotPaused` ~line 253)
- Create: `web/src/components/QueuedSection.tsx`
- Modify: the session-list host that renders `SessionList` (found via `grep -rn "<SessionList" web/src` — both mobile and desktop hosts) to partition `status === 'queued'` sessions into `<QueuedSection>` above the list
- Test: create `web/src/components/__tests__/QueuedSection.test.tsx`

**Interfaces:**
- Consumes: `queuePosition` + endpoints from Task 6.
- Produces: `Session.queuePosition?: number | null`; `AppSettings.maxActiveSessions: number | null`; hooks `useQueueMove()` (`{id, op, position?}`) and `useQueueCancel()` (`{id}`), both invalidating `QK.sessions` on success; `<QueuedSection sessions={Session[]} onOpen={(id)=>void}>`.

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const queueMove = vi.fn()
const queueCancel = vi.fn()
vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig()),
  useQueueMove: () => ({ mutate: queueMove }),
  useQueueCancel: () => ({ mutate: queueCancel }),
}))

import { QueuedSection } from '../QueuedSection'

const qs = (id: string, pos: number) => ({
  id, name: `sess-${id}`, status: 'queued', queuePosition: pos,
  account: '', repo: 'owner/repo', branch: null, updatedAt: '',
}) as never

describe('QueuedSection', () => {
  it('renders rows sorted by position with a count header', () => {
    render(<QueuedSection sessions={[qs('b', 2), qs('a', 1)]} onOpen={() => {}} />)
    expect(screen.getByText(/Queued \(2\)/)).toBeInTheDocument()
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('sess-a')
  })

  it('wires move/cancel controls', () => {
    render(<QueuedSection sessions={[qs('a', 1), qs('b', 2)]} onOpen={() => {}} />)
    fireEvent.click(screen.getAllByLabelText('Move up')[1])
    expect(queueMove).toHaveBeenCalledWith({ id: 'b', op: 'up' })
    fireEvent.click(screen.getAllByLabelText('Remove from queue')[0])
    expect(queueCancel).toHaveBeenCalledWith({ id: 'a' })
  })

  it('renders nothing when empty', () => {
    const { container } = render(<QueuedSection sessions={[]} onOpen={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run to verify failure** — `cd web && npx vitest run src/components/__tests__/QueuedSection.test.tsx` → module not found.

- [ ] **Step 3: Implement**

Types/hooks: add fields; mutations copy `useSetAutopilotPaused` (hooks.ts:253-259) exactly, endpoints `api.post(\`/sessions/${id}/queue-move\`, {op, position})` and `api.del(\`/sessions/${id}/queue\`)`.

`QueuedSection.tsx` — reuse `GroupHeader` (`web/src/components/GroupHeader.tsx`) for the collapsible header (`sticky`, chevron, count, `aria-expanded`; collapse state via local `useState`, persistence optional-NO for v1). Rows: `<ul>` of `<li>` — session name + repo label + position badge, then an actions group `<div role="group">` with icon buttons `aria-label="Move up" / "Move down" / "Move to top" / "Remove from queue"` calling the hooks; the row's open behavior is a separate button wrapping ONLY the name/label block (controls NEVER nested inside it). Match SessionCard's Tailwind classes for row chrome. Disable "Move up"/"Move to top" on position 1 and "Move down" on the last row.

Host wiring: in each host that renders `<SessionList sessions={...}>`, partition first:

```tsx
const queued = sessions.filter(s => s.status === 'queued')
  .sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0))
const live = sessions.filter(s => s.status !== 'queued')
```

render `<QueuedSection sessions={queued} onOpen={openSession} />` above the list and pass `live` to `SessionList`.

- [ ] **Step 4: Run to verify pass** — the new test file, then `npx vitest run` (full) — fix any of the 38 `vi.mock('@/api/hooks')` files that now fail because the mock object is missing `useQueueMove`/`useQueueCancel` (only files whose components transitively call them; the spread-orig mock pattern above avoids most breakage — use it for any file you must touch).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/components/QueuedSection.tsx web/src/components/__tests__/QueuedSection.test.tsx <touched hosts/tests>
git commit -m "feat(web): queued sidebar section with priority controls

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Web — queued placeholder state + settings field

**Files:**
- Modify: `web/src/components/session/SessionPlaceholder.tsx` (Phase union at line 31, JSX cases at lines 152-167), `web/src/components/session/SessionBody.tsx` (~line 164-170 placeholder selection), `web/src/components/settings/GeneralSection.tsx` (copy `maxArchivedSessions` pattern: state init lines 28-30, save lines 33-43, JSX lines 170-191)
- Test: extend `web/src/components/session/__tests__/SessionBody.test.tsx` (queued phase) and `web/src/components/settings/__tests__/GeneralSection.test.tsx` (field saves)

**Interfaces:**
- Consumes: `Session.queuePosition`, `AppSettings.maxActiveSessions`, `useUpdateSettings()` (hooks.ts:186-192).
- Produces: `Phase` union gains `'queued'`; SessionBody passes `phase="queued"` (with `queuePosition` for copy "Queued — position N") when `session.status === 'queued'`; settings numeric input labeled "Max active sessions", `min=1`, placeholder "5 (default)", saving `maxActiveSessions: parsed || null`.

- [ ] **Step 1: Write the failing tests** — SessionBody: render with a `status:'queued', queuePosition: 3` session, assert `screen.getByText(/Queued — position 3/)`; GeneralSection: type "7" into the labeled input, save, assert `updateSettings.mutate` called with `maxActiveSessions: 7` (copy the file's existing mock harness — it's in the 38-file `vi.mock` list).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — `'queued'` phase case in `SessionPlaceholder` (icon + "Queued — position N" + "Starts automatically when a slot frees"); SessionBody branch mirrors how the connecting/loading phase is chosen; GeneralSection field is a verbatim structural copy of the `maxArchivedSessions` block with the new label/key.

- [ ] **Step 4: Run to verify pass** — both files, then full `npx vitest run`, `npm run build`, `npx eslint src`.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/SessionPlaceholder.tsx web/src/components/session/SessionBody.tsx web/src/components/settings/GeneralSection.tsx <tests>
git commit -m "feat(web): queued session placeholder + max-active-sessions setting

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Gates + PR

- [ ] **Step 1: Full gates**

```bash
ruff check src/ server/
PYTHONPATH=.:src python3 -m mypy server/ --ignore-missing-imports
PYTHONPATH=.:src python3 -m pytest tests/ -q
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -q --ignore=server/tests/test_daemon_properties.py --deselect server/tests/test_eta_parity_live.py
cd web && npx vitest run && npx tsc --noEmit && npm run build && npx eslint src
```

Expected: clean except the documented pre-existing local failures (`test_archive.py` ×6, `test_copilot_proxy_resolve.py`).

- [ ] **Step 2: PR**

```bash
git push -u origin feat/max-active-sessions-queue
gh pr create --title "feat: global max-active-sessions cap with server-side priority queue" --body "$(cat <<'EOF'
## Summary
- Global cap on active sessions (default 5, `maxActiveSessions` setting, min 1) enforced in `create_session` for EVERY origin — UI and automations alike
- Overflow becomes real `status="queued"` session rows + a `session_queue` table (priority, enqueued_at, deferred spawn params); worktree + account decisions happen at dequeue
- Drain on slot-free (close/archive/driver-exit), cap raise, and daemon rehydrate; start failure isolates to `needs_attention` and the next entry proceeds
- `POST /api/sessions/{id}/queue-move` (up/down/top/position) + `DELETE /api/sessions/{id}/queue`, `queuePosition` in session payloads, RemoteOrchestrator/RPC proxies
- Sidebar "Queued (N)" collapsible section with reorder/remove controls (actions group, a11y-safe), queued placeholder state in the session view, settings field

Spec: docs/superpowers/specs/2026-07-21-max-active-sessions-queue-design.md

## Test plan
- server: session_queue model/CRUD, cap boundary + automations-queue-too, drain order/ties/failure/rehydrate, move/cancel ops, API + RPC breadth
- web: QueuedSection render/sort/controls, queued placeholder, settings field; full vitest + tsc + build + eslint

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
gh pr view --json state,isDraft,autoMergeRequest
```

Confirm non-draft + `autoMergeRequest` non-null before reporting done.
