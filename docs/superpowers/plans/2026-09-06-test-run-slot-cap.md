# Verification-Phase Test-Run Slot Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an orchestrator-level, per-host capacity cap on sessions that are running the full server-test suite ("verification phase"), so a Backlog Runner burst cannot pile up N concurrent full pytest runs on one host (the 2026-08-29 exit-137 incident).

**Architecture:** A new `test_run_slots` table records, per session, whether it currently **holds** a verification slot or is **waiting** for one, scoped by `host`. A pure CRUD module (`server/sessions/test_run_slots.py`, mirroring the existing `server/sessions/session_queue.py`) implements `acquire`/`release`/`status` against that table. A small FastAPI router (`server/api/test_slots.py`) exposes this over the loopback API so any session's Bash tool can `curl` it before/after running the full suite. The cap is configurable via `CCSWITCH_TEST_SLOT_CAP` (default 1). Every entry point fails OPEN: any exception (DB down, bug) is caught and treated as "granted" — a session must never be blocked from running tests by this control plane. `.agents/instructions/core.md` (source of `CLAUDE.md`/`AGENTS.md`) documents the acquire → poll → release workflow sessions must follow before a full `pytest server/tests/` run.

This is a **self-contained, in-orchestrator "verification-phase cap"** (issue #822's second candidate shape) — it deliberately does NOT depend on issue #821's `scripts/run-server-tests.sh` (unimplemented, out of scope here) or add a daemon-side RPC lease. It is a cooperative, self-reported gate: a session must call the API around its own full-suite run. True OS-level interception of arbitrary Bash commands is out of scope without #821.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2.x async ORM, aiosqlite (tests) / asyncpg (prod), pytest + pytest-asyncio.

**Spec:** GitHub issue steiner385/ccswitch#822 ("Backlog Runner: enforce a host-level cap on concurrent full-suite verification runs"). Design shape and #821-skip decision confirmed with the repo owner before planning (AskUserQuestion, this session).

## Global Constraints

- Configurable per host via the existing flat-env-var convention (no hostname-suffixed vars — each host's systemd unit sets its own env file; see `.agents/instructions/core.md` "Per-host config").
- Fail-open: unreachable/erroring control plane must never block a session from running tests, and must never deadlock.
- Surface queue state ("waiting for test slot, N ahead") in a way a queued session's own output makes visible — no new UI plumbing; the session's own Bash tool output already streams into its transcript.
- Constitution II (per-process isolation) is not touched by this change — no process spawning, only bookkeeping.
- Constitution V/III: TDD, and every new column must be reconcile-safe (nullable, PK, or a synthesizable default) unless added to `RECONCILE_EXEMPT` with a comment — this is a brand-new table, so `create_all` builds it fully-formed (no ALTER-ADD path needed), matching the `session_queue`/`notification_channel`/`hosts` precedent.

---

## File Structure

- `server/db/models.py` — add `TestRunSlot` model (new table `test_run_slots`), right after `SessionQueue`.
- `server/db/migrations/versions/0030_test_run_slots.py` — new migration (reviewable record only; `create_all` is what actually builds it).
- `server/tests/test_test_run_slots_model.py` — model roundtrip test.
- `server/tests/test_postgres_compat.py` — add `test_run_slots` to `_FK_LINKS` and `_REQUIRED_EXTRAS`.
- `server/tests/test_schema_reconcile_coverage.py` — add `test_run_slots.host` to `RECONCILE_EXEMPT` with a comment.
- `server/config.py` — add `test_slot_cap` / `CCSWITCH_TEST_SLOT_CAP` to both the pydantic `Settings` and the dataclass fallback `Settings`.
- `server/tests/test_config_test_slot_cap.py` — config knob test.
- `server/sessions/test_run_slots.py` — pure CRUD: `acquire`, `release`, `status`. No orchestrator imports.
- `server/tests/test_test_run_slots.py` — unit tests for capacity/queueing/promotion/idempotency.
- `server/api/test_slots.py` — FastAPI router: `POST /api/test-slots/acquire`, `POST /api/test-slots/release`, `GET /api/test-slots/status`. Fail-open at this layer (catches exceptions from the service module).
- `server/app.py` — import + `app.include_router(test_slots_router)`.
- `server/tests/test_test_slots_api.py` — HTTP contract tests: auth required, granted/queued/degraded response shapes, fail-open on a forced exception.
- `.agents/instructions/core.md` — new bullet under "Dev / testing" documenting the acquire/poll/release workflow. Regenerate `AGENTS.md` via `python3 .agents/agentctl.py render`.

---

### Task 1: DB model + migration + schema-safety entries

**Files:**
- Modify: `server/db/models.py` (add `TestRunSlot` after the `SessionQueue` class, ~line 477)
- Create: `server/db/migrations/versions/0030_test_run_slots.py`
- Modify: `server/tests/test_postgres_compat.py` (`_FK_LINKS`, `_REQUIRED_EXTRAS`, ~lines 203-243)
- Modify: `server/tests/test_schema_reconcile_coverage.py` (`RECONCILE_EXEMPT`, ~line 78)
- Create: `server/tests/test_test_run_slots_model.py`

**Interfaces:**
- Produces: `server.db.models.TestRunSlot` with columns `session_id: str` (PK, FK→`sessions.id`), `host: str`, `status: str` (`"holding"` | `"waiting"`), `requested_at: datetime` (tz-aware). Task 3 imports this model.

- [ ] **Step 1: Write the failing model test**

Create `server/tests/test_test_run_slots_model.py`:

```python
"""test_run_slots table roundtrip (#822 verification-phase test-run slot cap)."""
import asyncio


def test_test_run_slot_roundtrip(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        from sqlalchemy import select
        from server.db.models import TestRunSlot
        async with db_session() as db:
            db.add(TestRunSlot(session_id="s1", host="rocky", status="holding"))
        async with db_session() as db:
            row = (await db.execute(select(TestRunSlot))).scalar_one()
            assert row.session_id == "s1"
            assert row.host == "rocky"
            assert row.status == "holding"
            assert row.requested_at is not None

    asyncio.run(run())


def test_test_run_slot_status_default_is_waiting(tmp_path):
    """The Python-side default matters for the SQLite reconcile ALTER-ADD path
    (an existing row backfilled by reconcile gets this value, not NULL)."""
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't2.db'}")
        from sqlalchemy import select
        from server.db.models import TestRunSlot
        async with db_session() as db:
            db.add(TestRunSlot(session_id="s2", host="rocky"))
        async with db_session() as db:
            row = (await db.execute(select(TestRunSlot))).scalar_one()
            assert row.status == "waiting"

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_test_run_slots_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'TestRunSlot'`.

(Use the main checkout's prebuilt `.venv` per `.agents/instructions/core.md` — a fresh worktree has no built env. Run from the worktree path but with the main checkout's interpreter, and unset `CCSWITCH_SESSIOND_*`/`CCSWITCH_DAEMON_ROLE` first: `env -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_DAEMON_ROLE ...` — or simply `env | grep -i ccswitch` first to see what's actually set in this shell and strip it.)

- [ ] **Step 3: Add the `TestRunSlot` model**

In `server/db/models.py`, immediately after the `SessionQueue` class (after its closing `params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)` line), add:

```python
class TestRunSlot(Base):
    """Verification-phase test-run slot (#822): one row per session that is
    holding or waiting for a "running the full server-test suite" slot on a
    host. Capacity (rows with status='holding' per host) is enforced by
    server.sessions.test_run_slots against server.config.Settings.test_slot_cap;
    this table only records state — it enforces nothing on its own.

    Deleted when the session releases (or gives up); releasing a 'holding' row
    promotes the oldest 'waiting' row for the same host to 'holding'.
    """

    __tablename__ = "test_run_slots"

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.id"), primary_key=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    # "holding" (counts against the cap) | "waiting" (queued, no slot yet)
    status: Mapped[str] = mapped_column(String, nullable=False, default="waiting")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2.
Expected: PASS (2 tests).

- [ ] **Step 5: Add the migration (reviewable record only)**

Create `server/db/migrations/versions/0030_test_run_slots.py`:

```python
"""create test_run_slots — verification-phase test-run slot cap (#822)

Revision ID: 0030_test_run_slots
Revises: 0029_notification_rule_scope_session
Create Date: 2026-09-06

Deploy note: nothing runs ``alembic upgrade`` at deploy time — this is a NEW
table, so ``init_db``'s ``create_all`` builds it on every dialect at next boot
(reconcile is only needed for columns added to EXISTING tables). This revision
is the reviewable record of the schema change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_test_run_slots"
down_revision = "0029_notification_rule_scope_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_run_slots",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("test_run_slots")
```

- [ ] **Step 6: Add reconcile-safety + Postgres-compat entries**

In `server/tests/test_schema_reconcile_coverage.py`, add to `RECONCILE_EXEMPT` (after the `assistant_tokens.*` block, before the closing `}`):

```python
    # test_run_slots: a NEW table (create_all builds it fully-formed on dev SQLite;
    # Alembic 0030_test_run_slots builds it on prod). host is part of the initial
    # CREATE TABLE, never ALTER-added to a populated table, and mandatory at INSERT
    # (a slot row is meaningless without the host it was requested on), so no
    # synthesizable default.
    "test_run_slots.host",
```

In `server/tests/test_postgres_compat.py`, add to `_REQUIRED_EXTRAS` (after the `assistant_tokens` entry):

```python
    # test_run_slots joins the sweep via requested_at (DateTime); session_id is the
    # FK-linked PK (see _FK_LINKS). host is NOT NULL with no synthesizable default.
    "test_run_slots": {"host": "dtcheck-host"},
```

and to `_FK_LINKS` (after the `session_queue` entry):

```python
    "test_run_slots": {"session_id": "sessions"},
```

- [ ] **Step 7: Run the reconcile-coverage test (fast, not Postgres-gated) and the model test**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_schema_reconcile_coverage.py /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_test_run_slots_model.py -v`
Expected: PASS. (`test_postgres_compat.py`'s new entries are exercised only when `CCSWITCH_TEST_PG_URL` is set — not required to pass locally, but must not break the *unconditional* collection/import of that file. Sanity-check with `.venv/bin/python -m pytest server/tests/test_postgres_compat.py --collect-only -q` to confirm it still imports cleanly.)

- [ ] **Step 8: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0030_test_run_slots.py \
  server/tests/test_test_run_slots_model.py server/tests/test_postgres_compat.py \
  server/tests/test_schema_reconcile_coverage.py
git commit -m "$(cat <<'EOF'
feat(db): add test_run_slots table for the verification-phase slot cap (#822)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `CCSWITCH_TEST_SLOT_CAP` config knob

**Files:**
- Modify: `server/config.py` (pydantic `Settings`, after `self_model_switch_cap` ~line 240; dataclass fallback `Settings`, after `self_model_switch_cap` ~line 516)
- Create: `server/tests/test_config_test_slot_cap.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `server.config.get_settings().test_slot_cap: int` (default `1`), read via `CCSWITCH_TEST_SLOT_CAP`. Task 4's API router calls `get_settings().test_slot_cap`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_config_test_slot_cap.py`:

```python
"""CCSWITCH_TEST_SLOT_CAP — verification-phase test-run slot cap (#822)."""
from __future__ import annotations


def test_default_cap_is_one(monkeypatch):
    monkeypatch.delenv("CCSWITCH_TEST_SLOT_CAP", raising=False)
    from server.config import get_settings
    assert get_settings().test_slot_cap == 1


def test_cap_reads_from_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "3")
    from server.config import get_settings
    assert get_settings().test_slot_cap == 3


def test_bad_cap_value_falls_back_to_default_on_dataclass_path(monkeypatch):
    """Only meaningful on the pydantic-settings-unavailable fallback path (see
    server/config.py's ``except ImportError`` block) — a non-digit string there
    is guarded explicitly (mirrors CCSWITCH_SELF_MODEL_SWITCH_CAP). Under normal
    pydantic-settings installs a non-int value raises at Settings() construction,
    which is also an acceptable (loud) failure mode for a malformed operator env.
    """
    import server.config as config_mod
    if not hasattr(config_mod, "BaseSettings"):
        monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "not-a-number")
        assert config_mod.get_settings().test_slot_cap == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_config_test_slot_cap.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'test_slot_cap'`.

- [ ] **Step 3: Add the field to both `Settings` variants**

In `server/config.py`, in the pydantic `Settings` class, immediately after:

```python
        self_model_switch_cap: int = Field(
            default=3, alias="CCSWITCH_SELF_MODEL_SWITCH_CAP")
```

add:

```python
        # Verification-phase test-run slot cap (#822): max sessions per host that
        # may simultaneously hold a "running the full server suite" slot. Keeps a
        # Backlog Runner burst from piling up N concurrent full pytest runs (the
        # 2026-08-29 exit-137 incident, #821/#822). Fail-open by design — see
        # server/sessions/test_run_slots.py and server/api/test_slots.py.
        test_slot_cap: int = Field(default=1, alias="CCSWITCH_TEST_SLOT_CAP")
```

In the dataclass fallback `Settings` class, immediately after:

```python
        self_model_switch_cap: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_SELF_MODEL_SWITCH_CAP", "")) and v.isdigit() else 3
        )
```

add:

```python
        test_slot_cap: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_TEST_SLOT_CAP", "")) and v.isdigit() else 1
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2.
Expected: PASS (3 tests; the third is a no-op assertion under pydantic-settings, which is fine).

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_config_test_slot_cap.py
git commit -m "$(cat <<'EOF'
feat(config): add CCSWITCH_TEST_SLOT_CAP knob (#822)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Service module — `server/sessions/test_run_slots.py`

**Files:**
- Create: `server/sessions/test_run_slots.py`
- Create: `server/tests/test_test_run_slots.py`

**Interfaces:**
- Consumes: `server.db.models.TestRunSlot` (Task 1), `server.db.db_session` (existing).
- Produces (Task 4 imports these exactly):
  - `async def acquire(session_id: str, host: str, *, cap: int) -> tuple[bool, int]` — returns `(granted, ahead)`. `ahead` is the count of waiters queued before this session (0 when granted).
  - `async def release(session_id: str) -> None` — idempotent; no-op if the session holds no row.
  - `async def status(session_id: str, host: str) -> tuple[str, int]` — returns `(state, ahead)` where `state` is `"holding"`, `"waiting"`, or `"none"`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_test_run_slots.py`:

```python
"""server.sessions.test_run_slots — verification-phase slot cap (#822).

Real SQLite via init_db(), one asyncio.run() per test (mirrors
test_session_queue_model.py) — no ASGI/TestClient involved here.
"""
from __future__ import annotations

import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_acquire_grants_under_cap(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        granted, ahead = await trs.acquire("s1", "rocky", cap=2)
        assert (granted, ahead) == (True, 0)

    _run(go())


def test_acquire_queues_over_cap(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("s1", "rocky", cap=1))[0] is True
        granted2, ahead2 = await trs.acquire("s2", "rocky", cap=1)
        assert (granted2, ahead2) == (False, 0)
        granted3, ahead3 = await trs.acquire("s3", "rocky", cap=1)
        assert (granted3, ahead3) == (False, 1)

    _run(go())


def test_acquire_is_idempotent_for_the_same_session(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        first = await trs.acquire("s1", "rocky", cap=1)
        second = await trs.acquire("s1", "rocky", cap=1)
        assert first == second == (True, 0)

        await trs.acquire("s2", "rocky", cap=1)  # queued
        waiting_again = await trs.acquire("s2", "rocky", cap=1)
        assert waiting_again == (False, 0)

    _run(go())


def test_release_promotes_oldest_waiter(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.acquire("s1", "rocky", cap=1)
        await trs.acquire("s2", "rocky", cap=1)  # waiting, ahead=0
        await trs.acquire("s3", "rocky", cap=1)  # waiting, ahead=1

        await trs.release("s1")

        assert (await trs.status("s2", "rocky")) == ("holding", 0)
        assert (await trs.status("s3", "rocky")) == ("waiting", 0)

    _run(go())


def test_release_of_a_waiter_does_not_promote_anyone(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.acquire("s1", "rocky", cap=1)
        await trs.acquire("s2", "rocky", cap=1)  # waiting

        await trs.release("s2")

        assert (await trs.status("s1", "rocky")) == ("holding", 0)
        assert (await trs.status("s2", "rocky")) == ("none", 0)

    _run(go())


def test_release_is_idempotent(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.release("never-acquired")  # must not raise
        await trs.acquire("s1", "rocky", cap=1)
        await trs.release("s1")
        await trs.release("s1")  # must not raise

    _run(go())


def test_status_of_unknown_session_is_none(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.status("nope", "rocky")) == ("none", 0)

    _run(go())


def test_hosts_are_independent(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("s1", "rocky", cap=1))[0] is True
        # Different host, same cap=1: unaffected by rocky's saturation.
        assert (await trs.acquire("s2", "dobby", cap=1))[0] is True

    _run(go())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_test_run_slots.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.test_run_slots'`.

- [ ] **Step 3: Implement the service module**

Create `server/sessions/test_run_slots.py`:

```python
"""Pure CRUD for the verification-phase test-run slot cap (#822).

One row per session that currently holds or is waiting for a "running the
full server-test suite" slot, keyed by host. Capacity is the count of
status="holding" rows for that host, checked against a caller-supplied cap
(server.config.Settings.test_slot_cap) — this module has no opinion on the
cap value itself. No orchestrator imports: callers call this module, never
vice versa (mirrors server/sessions/session_queue.py).

Every function here can raise (a normal DB error propagates from db_session).
Fail-open behavior — treating an error as "granted"/"released" so a session is
never blocked from running tests — is the CALLER's responsibility (see
server/api/test_slots.py), not this module's: a pure CRUD layer that silently
swallowed errors would make callers unable to tell a real failure from a
successful no-op.
"""
from __future__ import annotations

from sqlalchemy import func, select

from server.db import db_session
from server.db.models import TestRunSlot

HOLDING = "holding"
WAITING = "waiting"


async def _ahead(db, host: str, requested_at) -> int:
    return (await db.execute(
        select(func.count()).select_from(TestRunSlot)
        .where(TestRunSlot.host == host, TestRunSlot.status == WAITING,
               TestRunSlot.requested_at < requested_at)
    )).scalar_one()


async def acquire(session_id: str, host: str, *, cap: int) -> tuple[bool, int]:
    """Try to grant a slot; else queue. Returns (granted, ahead).

    ``ahead`` is the number of earlier waiters when queued (always 0 when
    granted). Idempotent: calling again for a session that already holds or
    is waiting just re-reads its current state instead of creating a second row.
    """
    async with db_session() as db:
        existing = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if existing is not None:
            if existing.status == HOLDING:
                return True, 0
            return False, await _ahead(db, host, existing.requested_at)

        holding_count = (await db.execute(
            select(func.count()).select_from(TestRunSlot)
            .where(TestRunSlot.host == host, TestRunSlot.status == HOLDING)
        )).scalar_one()
        if holding_count < cap:
            db.add(TestRunSlot(session_id=session_id, host=host, status=HOLDING))
            return True, 0

        row = TestRunSlot(session_id=session_id, host=host, status=WAITING)
        db.add(row)
        await db.flush()
        return False, await _ahead(db, host, row.requested_at)


async def release(session_id: str) -> None:
    """Drop this session's row. If it held a slot, promote the oldest waiter
    for its host (if any) from 'waiting' to 'holding'. No-op if the session
    holds no row (already released, or never acquired)."""
    async with db_session() as db:
        row = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if row is None:
            return
        was_holding = row.status == HOLDING
        host = row.host
        await db.delete(row)
        await db.flush()
        if not was_holding:
            return
        next_waiter = (await db.execute(
            select(TestRunSlot)
            .where(TestRunSlot.host == host, TestRunSlot.status == WAITING)
            .order_by(TestRunSlot.requested_at)
            .limit(1)
        )).scalar_one_or_none()
        if next_waiter is not None:
            next_waiter.status = HOLDING


async def status(session_id: str, host: str) -> tuple[str, int]:
    """Current state for this session: ("holding", 0) | ("waiting", ahead) |
    ("none", 0) — the last means no row (never acquired, or already released)."""
    async with db_session() as db:
        row = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if row is None:
            return "none", 0
        if row.status == HOLDING:
            return "holding", 0
        return "waiting", await _ahead(db, host, row.requested_at)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (8 tests).

- [ ] **Step 5: Run ruff + mypy on the new module**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m ruff check /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/sessions/test_run_slots.py && .venv/bin/python -m mypy /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/sessions/test_run_slots.py --ignore-missing-imports`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/test_run_slots.py server/tests/test_test_run_slots.py
git commit -m "$(cat <<'EOF'
feat(sessions): add test_run_slots acquire/release/status CRUD (#822)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: API router, app wiring, docs

**Files:**
- Create: `server/api/test_slots.py`
- Modify: `server/app.py` (import ~line 51, `include_router` ~line 845)
- Create: `server/tests/test_test_slots_api.py`
- Modify: `.agents/instructions/core.md` (new bullet under "Dev / testing", after the "CI runs the server suite..." bullet ~line 87)
- Regenerate: `AGENTS.md` (via `agentctl render`)

**Interfaces:**
- Consumes: `server.sessions.test_run_slots.{acquire,release,status}` (Task 3), `server.config.get_settings().test_slot_cap` (Task 2).
- Produces: `server.api.test_slots.router` (FastAPI `APIRouter`), mounted at `/api/test-slots`.

- [ ] **Step 1: Write the failing API tests**

Create `server/tests/test_test_slots_api.py`:

```python
"""HTTP contract for /api/test-slots/{acquire,release,status} (#822).

Bare-FastAPI + init_db pattern (mirrors test_client_logs_api.py), since this
router owns its own DB access (via server.sessions.test_run_slots) rather
than taking a get_db dependency, and needs no orchestrator.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    from server.db import init_db
    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"))

    from server.api.test_slots import router
    from server.auth.middleware import current_identity

    app = FastAPI()
    app.dependency_overrides[current_identity] = lambda: "tester@example.com"
    app.include_router(router)
    return TestClient(app)


def test_acquire_grants_when_under_cap(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    r = client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["ahead"] == 0
    assert body["cap"] == 1
    assert body["degraded"] is False


def test_acquire_queues_over_cap(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    r = client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})
    body = r.json()
    assert body["granted"] is False
    assert body["ahead"] == 0


def test_status_reflects_waiting_position(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})
    r = client.get("/api/test-slots/status", params={"sessionId": "s2", "host": "rocky"})
    assert r.status_code == 200
    assert r.json() == {"status": "waiting", "ahead": 0, "cap": 1, "degraded": False}


def test_release_promotes_next_waiter(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})

    r = client.post("/api/test-slots/release", json={"sessionId": "s1"})
    assert r.status_code == 200
    assert r.json() == {"released": True}

    r2 = client.get("/api/test-slots/status", params={"sessionId": "s2", "host": "rocky"})
    assert r2.json()["status"] == "holding"


def test_acquire_fails_open_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "acquire", boom)

    r = client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["degraded"] is True


def test_status_fails_open_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "status", boom)

    r = client.get("/api/test-slots/status", params={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unknown"
    assert body["degraded"] is True


def test_release_never_raises_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "release", boom)

    r = client.post("/api/test-slots/release", json={"sessionId": "s1"})
    assert r.status_code == 200
    assert r.json() == {"released": True}


def test_requires_auth():
    from server.api.test_slots import router
    app = FastAPI()
    app.include_router(router)  # no dependency override → real auth
    r = TestClient(app).post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_test_slots_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.api.test_slots'`.

- [ ] **Step 3: Implement the router**

Create `server/api/test_slots.py`:

```python
"""Verification-phase test-run slot API (#822).

Any session may call this around its own full-suite `pytest server/tests/`
run to cooperate with the per-host cap documented in
.agents/instructions/core.md ("Dev / testing"): acquire before running the
suite, poll status while queued, release when done (in a shell `trap ... EXIT`
so a crash still frees the slot on the DAEMON side once the session's process
tree exits and nothing else releases it — see that doc for the exact recipe).

Fail-open by design: ANY exception from server.sessions.test_run_slots is
caught here and turned into "you may proceed" (acquire → granted=True,
status → "unknown", release → best-effort). A session must never be blocked
from running tests by this control plane being unavailable.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.config import get_settings
from server.sessions import test_run_slots

router = APIRouter(prefix="/api/test-slots", tags=["test-slots"])
logger = logging.getLogger(__name__)


class AcquireBody(BaseModel):
    sessionId: str
    host: str


class ReleaseBody(BaseModel):
    sessionId: str


@router.post("/acquire")
async def acquire_test_slot(
    body: AcquireBody, _identity: str = Depends(current_identity)
) -> dict:
    cap = get_settings().test_slot_cap
    try:
        granted, ahead = await test_run_slots.acquire(body.sessionId, body.host, cap=cap)
    except Exception:
        logger.exception(
            "test-slot acquire failed for session=%s host=%s — failing open",
            body.sessionId, body.host)
        return {"granted": True, "ahead": 0, "cap": cap, "degraded": True}
    return {"granted": granted, "ahead": ahead, "cap": cap, "degraded": False}


@router.post("/release")
async def release_test_slot(
    body: ReleaseBody, _identity: str = Depends(current_identity)
) -> dict:
    try:
        await test_run_slots.release(body.sessionId)
    except Exception:
        logger.exception("test-slot release failed for session=%s", body.sessionId)
    return {"released": True}


@router.get("/status")
async def test_slot_status(
    sessionId: str, host: str, _identity: str = Depends(current_identity)
) -> dict:
    cap = get_settings().test_slot_cap
    try:
        state, ahead = await test_run_slots.status(sessionId, host)
    except Exception:
        logger.exception(
            "test-slot status failed for session=%s host=%s — failing open",
            sessionId, host)
        return {"status": "unknown", "ahead": 0, "cap": cap, "degraded": True}
    return {"status": state, "ahead": ahead, "cap": cap, "degraded": False}
```

- [ ] **Step 4: Wire the router into the app**

In `server/app.py`, add the import next to the other `server.api.*` router imports (after `from server.api.pm import router as pm_router` — check the exact neighboring line first with `grep -n "from server.api" server/app.py`, insert alphabetically-adjacent is fine, ordering isn't enforced):

```python
from server.api.test_slots import router as test_slots_router
```

and add the registration next to the other `include_router` calls (after `app.include_router(pm_router)`):

```python
    app.include_router(test_slots_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: same command as Step 2, plus a quick app-import sanity check:

`cd /home/tony/GitHub/ccswitch && .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/test_test_slots_api.py -v && .venv/bin/python -c "from server.app import create_app; create_app()"`

Expected: 8 tests PASS; app import succeeds (unset `CCSWITCH_SESSIOND_*`/`CCSWITCH_DAEMON_ROLE` first per the "Dev / testing" note, and set `CCSWITCH_DEV=1 CCSWITCH_DB_URL=sqlite+aiosqlite:////tmp/test-slots-sanity.db` for the import check).

- [ ] **Step 6: Document the workflow in `.agents/instructions/core.md`**

In `.agents/instructions/core.md`, in the "## Dev / testing" section, immediately after this existing bullet:

```markdown
- CI runs the server suite as `pytest server/tests/ -n auto --dist loadfile --timeout=300`. Serial
  it takes ~25 min; run it in the background with output tee'd to `/tmp/`, and never two full runs
  at once — the load spike gets the Bash call SIGKILLed (exit 137).
```

insert:

```markdown
- **Test-run slot cap (#822):** before starting a full `pytest server/tests/` run on a shared host
  (rocky), acquire a slot so concurrent Backlog Runner sessions don't pile up N full suites at once
  (`CCSWITCH_TEST_SLOT_CAP`, default 1 per host):
  ```bash
  sid="${CCSWITCH_SESSION_ID:-$$}"; host="$(hostname -s)"
  release() { curl -sf -X POST http://127.0.0.1:8787/api/test-slots/release \
    -H 'Content-Type: application/json' -d "{\"sessionId\":\"$sid\"}" >/dev/null 2>&1 || true; }
  trap release EXIT
  while :; do
    resp="$(curl -sf -X POST http://127.0.0.1:8787/api/test-slots/acquire \
      -H 'Content-Type: application/json' \
      -d "{\"sessionId\":\"$sid\",\"host\":\"$host\"}" 2>/dev/null)" || { echo "test-slot API unreachable — proceeding (fail-open)"; break; }
    granted="$(echo "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["granted"])' 2>/dev/null)"
    [ "$granted" = "True" ] && break
    ahead="$(echo "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ahead"])' 2>/dev/null)"
    echo "waiting for test slot on $host, $ahead ahead"
    sleep 15
  done
  ```
  This is a cooperative, self-reported gate (not OS-level process interception) — every call is
  fail-open by design (`server/api/test_slots.py`): if the API errors or is unreachable, proceed
  anyway rather than blocking. `release` runs on EXIT so a crashed/interrupted run still frees the
  slot for the next waiter.
```

- [ ] **Step 7: Regenerate `AGENTS.md`**

Run: `cd /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32 && python3 .agents/agentctl.py render`

Expected: `AGENTS.md` updated to include the new bullet (diff it to confirm: `git diff AGENTS.md`). Also run `python3 .agents/agentctl.py check` if that subcommand validates consistency, and fix anything it flags.

- [ ] **Step 8: Run the full four-step lint-and-test gate on the changed files**

Run:
```bash
cd /home/tony/GitHub/ccswitch
.venv/bin/python -m ruff check /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/api/test_slots.py /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/sessions/test_run_slots.py /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/config.py /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/db/models.py /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/app.py
.venv/bin/python -m mypy /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server --ignore-missing-imports
```
Expected: both clean (mypy against the whole `server/` package, per the gate's real invocation — new-file-only mypy runs can miss cross-module issues).

- [ ] **Step 9: Commit**

```bash
git add server/api/test_slots.py server/app.py server/tests/test_test_slots_api.py \
  .agents/instructions/core.md AGENTS.md
git commit -m "$(cat <<'EOF'
feat(api): add /api/test-slots + document the verification-phase workflow (#822)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Final Verification (before opening the PR)

- [ ] Run the full local suite in the background, tee'd to `/tmp/`:
  `cd /home/tony/GitHub/ccswitch && nohup .venv/bin/python -m pytest /home/tony/GitHub/.ccswitch/worktrees/ccswitch/8cfc2e32/server/tests/ -x -q > /tmp/ccswitch-test-slot-cap-suite.log 2>&1 &` — then check it later; do not run a second full suite concurrently.
- [ ] Run `pytest tests/` (core CLI suite) with `PYTHONPATH` unaffected (this feature touches nothing under `src/ccswitch/`, so this should be a no-op check).
- [ ] `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` clean (the actual CI gate commands, not scoped subsets).
- [ ] Confirm `git diff AGENTS.md` only contains the intended new bullet (no unrelated drift from a stale render).
