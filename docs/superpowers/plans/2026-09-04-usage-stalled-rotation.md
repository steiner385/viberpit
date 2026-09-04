# Usage-Stalled Session Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions stalled because their account spent its usage get a visible "waiting for capacity" state, a one-click "next best account" recovery (per session and in batch), an unpin control, and tighter automation (weekly ceiling counts, `limited` sessions resume on reset, sweep fires when an account recovers).

**Architecture:** Two nullable columns (`paused_reason`, `paused_until`) ride the existing `_persist_status` single-writer path to the row and the WS `status` frame. A new `rotate_to_best` orchestrator method is a thin caller of the existing `_reassign` swap primitive (no new recovery path). The UI reads a server-derived `usageStalled` flag and adds a toolbar batch button, a session-menu item, and an unpin button.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (server), React + TypeScript + TanStack Query + vitest (web), pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-usage-stalled-rotation-design.md`

## Global Constraints

- Branch: `feat/usage-stalled-rotate` (already created off `origin/master`). Never commit to `master`.
- Worktree: `/home/tony/GitHub/.ccswitch/worktrees/ccswitch/fcd0f2a2`. Keep the shell cwd at the repo root; run web commands as `(cd web && …)` subshells (a persistent `cd web` breaks the PreToolUse hook).
- Python test runner (the worktree has no venv; the shared venv imports `ccswitch.*` from the deploy worktree unless `PYTHONPATH` points here; daemon env vars must be unset for server tests):

  ```bash
  PYT="env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_TLS_CA -u CCSWITCH_SESSIOND_TLS_CERT -u CCSWITCH_SESSIOND_TLS_KEY -u CCSWITCH_SESSIOND_CLIENT_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DB_URL CCSWITCH_DEV=1 PYTHONPATH=/home/tony/GitHub/.ccswitch/worktrees/ccswitch/fcd0f2a2/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest -p no:cacheprovider"
  ```

  Use `$PYT <paths> -q` everywhere below. Confirm once with
  `$PYT --co -q tests/test_selector.py | tail -1` that collection works.
- Web deps: `[ -e web/node_modules ] || ln -s /home/tony/GitHub/ccswitch/web/node_modules web/node_modules`.
- Commit messages: write to `/tmp/<task>-msg.txt` and commit with `git commit -F` (the Bash hook denies commands whose text pairs a deploy verb with a prod word). End every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Status vocabulary: the only new reason token is `pinned_account_limited`; `USAGE_STALL_REASONS = {"all_accounts_busy", "rotation_cap", "pinned_account_limited"}`. New reassign reasons: `manual_rotate`, `limit_reset`, `unpinned`.
- Wire field names (both serializers): `pausedReason`, `pausedUntil` (ISO, `iso_utc`), `usageStalled` (bool).
- HTTP: `POST /sessions/{id}/rotate`, `POST /sessions/batch/rotate`, `POST /sessions/{id}/account-pin` `{pinned: bool}`.
- Copy: toolbar button `Rotate stalled (N)`; menu item `Next best account`; pin button `aria-label="Unpin account"`; pills `Waiting for capacity` / `Usage limit (pinned)` with ` · retries <time>` / ` · resets <time>` suffixes from `formatResetTime`.
- Add new DB columns **nullable** (reconcile picks them up on both dialects). No Postgres-suite list entries are needed.
- Running the FULL vitest suite is mandatory in Tasks 8–10: a hook added to a shared component breaks tests that manually mock `@/api/hooks`.

---

### Task 1: `at_ceiling` helper in the core selector

**Files:**
- Modify: `src/ccswitch/selector.py:137-152` (inline ceiling check in `rank_accounts`)
- Test: `tests/test_selector.py`

**Interfaces:**
- Produces: `ccswitch.selector.at_ceiling(u: Usage, ceiling: float) -> bool` — True when `u.session_pct >= ceiling or u.weekly_pct >= ceiling`. `ceiling` is in percent (100.0), matching `_ROTATION_CEILING`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_selector.py` inside `class TestSelector` (it uses `unittest`; `usage()` comes from `tests.helpers`):

```python
    def test_at_ceiling_session_dimension(self):
        from ccswitch.selector import at_ceiling
        self.assertTrue(at_ceiling(usage(session=100.0, weekly=10.0), 100.0))

    def test_at_ceiling_weekly_dimension(self):
        from ccswitch.selector import at_ceiling
        self.assertTrue(at_ceiling(usage(session=0.0, weekly=100.0), 100.0))

    def test_at_ceiling_false_below_both(self):
        from ccswitch.selector import at_ceiling
        self.assertFalse(at_ceiling(usage(session=99.0, weekly=99.0), 100.0))

    def test_at_ceiling_respects_lower_ceiling(self):
        from ccswitch.selector import at_ceiling
        self.assertTrue(at_ceiling(usage(session=90.0, weekly=10.0), 90.0))
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT tests/test_selector.py -q -k at_ceiling`
Expected: 4 failures, `ImportError: cannot import name 'at_ceiling'`.

- [ ] **Step 3: Implement**

In `src/ccswitch/selector.py`, add after `_perishability` (before `rank_accounts`):

```python
def at_ceiling(u: Usage, ceiling: float) -> bool:
    """True when either usage dimension has reached *ceiling* (percent).

    The ONE definition of "this account has no headroom": rank_accounts drops such
    accounts for new picks, and the server's rotation monitor / crash recovery use
    it to decide a live session's account is dead. The weekly dimension counts —
    a session on a weekly-exhausted account 429s on its next turn exactly like a
    session-exhausted one.
    """
    return u.session_pct >= ceiling or u.weekly_pct >= ceiling
```

Then replace line 137 (`at_ceiling = u.session_pct >= ceiling or u.weekly_pct >= ceiling`) and the `if at_ceiling:` on the next line with `if at_ceiling(u, ceiling):` (the local variable shadowed the function name; delete the local).

- [ ] **Step 4: Run the selector tests**

Run: `$PYT tests/test_selector.py tests/test_selector_copilot_parity.py -q`
Expected: all pass (the 4 new ones included).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/selector.py tests/test_selector.py
printf 'feat(selector): at_ceiling helper — one definition of "no headroom"\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t1-msg.txt
git commit -q -F /tmp/t1-msg.txt
```

---

### Task 2: Pause reason on the record, the row and both serializers

**Files:**
- Create: `server/sessions/usage_stall.py`
- Create: `server/db/migrations/versions/0021_session_pause_reason.py`
- Modify: `server/db/models.py:199` (after `account_pinned`)
- Modify: `server/sessions/types.py` (fields, `to_dict`, `from_dict`)
- Modify: `server/sessions/orchestrator.py:6274-6323` (`_record_from_row`)
- Modify: `server/api/sessions.py:225` (`_record_to_dict`, next to `accountPinned`)
- Test: `server/tests/test_usage_stall_wire.py`

**Interfaces:**
- Produces: `server.sessions.usage_stall.USAGE_STALL_REASONS: frozenset[str]` and `is_usage_stalled(status: str, paused_reason: str | None) -> bool`.
- Produces: `SessionRecord.paused_reason: str | None`, `SessionRecord.paused_until: datetime | None`; DB `Session.paused_reason`, `Session.paused_until`; wire keys `pausedReason`, `pausedUntil`, `usageStalled`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_usage_stall_wire.py`:

```python
"""pausedReason / pausedUntil / usageStalled reach BOTH serializers and round-trip."""
from __future__ import annotations

import datetime as _dt

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord
from server.sessions.usage_stall import USAGE_STALL_REASONS, is_usage_stalled


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id="c",
                cols=80, rows=24, cwd="/tmp", status="idle")
    base.update(kw)
    return SessionRecord(**base)  # type: ignore[arg-type]


def test_stall_reasons_are_the_three_usage_tokens():
    assert USAGE_STALL_REASONS == frozenset(
        {"all_accounts_busy", "rotation_cap", "pinned_account_limited"})


def test_is_usage_stalled_table():
    assert is_usage_stalled("limited", None) is True
    assert is_usage_stalled("needs_attention", "all_accounts_busy") is True
    assert is_usage_stalled("needs_attention", "rotation_cap") is True
    assert is_usage_stalled("needs_attention", "crash_cap") is False
    assert is_usage_stalled("needs_attention", None) is False
    assert is_usage_stalled("idle", "all_accounts_busy") is False


def test_record_to_dict_emits_stall_fields():
    until = _dt.datetime(2026, 9, 4, 16, 10, 0)
    d = _record_to_dict(_rec(status="needs_attention", paused_reason="all_accounts_busy",
                             paused_until=until))
    assert d["pausedReason"] == "all_accounts_busy"
    assert d["pausedUntil"] == "2026-09-04T16:10:00+00:00"
    assert d["usageStalled"] is True


def test_record_to_dict_defaults():
    d = _record_to_dict(_rec())
    assert d["pausedReason"] is None
    assert d["pausedUntil"] is None
    assert d["usageStalled"] is False


def test_to_dict_from_dict_round_trip():
    until = _dt.datetime(2026, 9, 4, 16, 10, 0)
    r = _rec(status="limited", paused_reason="pinned_account_limited", paused_until=until)
    d = r.to_dict()
    assert d["usageStalled"] is True
    back = SessionRecord.from_dict(d)
    assert back.paused_reason == "pinned_account_limited"
    assert back.paused_until is not None
    assert back.paused_until.replace(tzinfo=None) == until
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_usage_stall_wire.py -q`
Expected: `ModuleNotFoundError: No module named 'server.sessions.usage_stall'`.

- [ ] **Step 3: Create the helper module**

Create `server/sessions/usage_stall.py`:

```python
"""The ONE definition of "this session is stalled on usage" (constitution VI).

A paused session collapses every cause into the single ``needs_attention`` status;
the persisted ``paused_reason`` token is what distinguishes "every account is at its
ceiling until 12:10" (auto-recoverable, worth a batch rotate) from a crash cap. Both
wire serializers (SessionRecord.to_dict and api/sessions._record_to_dict) emit the
derived flag, and rotate_to_best / the recovery sweep read it — nothing re-derives it.
"""
from __future__ import annotations

# _pause_session reasons that mean "no account had headroom", plus the pinned-at-
# ceiling overlay the rotation monitor sets on a `limited` session.
USAGE_STALL_REASONS: frozenset[str] = frozenset(
    {"all_accounts_busy", "rotation_cap", "pinned_account_limited"})


def is_usage_stalled(status: str, paused_reason: str | None) -> bool:
    if status == "limited":
        return True
    return status == "needs_attention" and paused_reason in USAGE_STALL_REASONS
```

- [ ] **Step 4: Add the record fields and wire keys**

In `server/sessions/types.py`, after the `interrupted_from_status` field (line 117) add:

```python
    # Why the session is parked (a _pause_session reason token, or
    # "pinned_account_limited" for the `limited` overlay) and the earliest known
    # usage reset (naive UTC). Persisted so a poll/reload keeps the cause; cleared by
    # _persist_status on any non-stalled status. See server/sessions/usage_stall.py.
    paused_reason: str | None = None
    paused_until: datetime | None = None
```

In `to_dict`, after the `"resumable"` entry add:

```python
            "pausedReason": self.paused_reason,
            "pausedUntil": iso_utc(self.paused_until),
            "usageStalled": is_usage_stalled(self.status, self.paused_reason),
```

with `from server.sessions.usage_stall import is_usage_stalled` at module top (no cycle: `usage_stall` imports nothing).

In `from_dict`, add to the constructor call:

```python
            paused_reason=d.get("pausedReason"),
            paused_until=(datetime.fromisoformat(d["pausedUntil"])
                          if d.get("pausedUntil") else None),
```

- [ ] **Step 5: Add the DB columns and migration**

In `server/db/models.py` after the `account_pinned` column (line 199):

```python
    # Why the session is parked + the earliest known usage reset (naive UTC), written
    # by _persist_status alongside `needs_attention`/`limited` and cleared on any other
    # status. Lets the dashboard say "waiting for capacity · retries 12:10" instead of
    # a bare "Turn failed". Nullable so reconcile ALTER-ADDs them on both dialects.
    paused_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
```

Create `server/db/migrations/versions/0021_session_pause_reason.py`:

```python
"""Persisted pause reason + earliest reset for usage-stalled sessions.

Spec: docs/superpowers/specs/2026-09-04-usage-stalled-rotation-design.md. Both columns
are nullable so ``_reconcile_columns`` applies them at startup on SQLite and Postgres;
this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_session_pause_reason"
down_revision = "0020_journal_mutation_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("paused_reason", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("paused_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "paused_until")
    op.drop_column("sessions", "paused_reason")
```

- [ ] **Step 6: Map the row and the HTTP serializer**

In `server/sessions/orchestrator.py` `_record_from_row`, after `host=getattr(row, "host", None),` add:

```python
            paused_reason=getattr(row, "paused_reason", None),
            paused_until=getattr(row, "paused_until", None),
```

In `server/api/sessions.py` `_record_to_dict`, after the `"accountPinned"` line add:

```python
        # Why a needs_attention/limited session is parked, when it may recover, and
        # the derived "stalled on usage" flag the Rotate-stalled batch button counts.
        # Same two-serializer rule as `resumable`: SessionRecord.to_dict is the
        # daemon→edge RPC copy, this is the browser copy — both must emit them.
        "pausedReason": getattr(record, "paused_reason", None),
        "pausedUntil": iso_utc(getattr(record, "paused_until", None)),
        "usageStalled": is_usage_stalled(record.status, getattr(record, "paused_reason", None)),
```

with `from server.sessions.usage_stall import is_usage_stalled` added to the imports (`iso_utc` is already imported there — verify with `grep -n iso_utc server/api/sessions.py | head -2`).

- [ ] **Step 7: Run the tests**

Run: `$PYT server/tests/test_usage_stall_wire.py server/tests/test_record_to_dict_pin.py server/tests/test_resume_interrupted.py server/tests/test_schema_reconcile_coverage.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/usage_stall.py server/sessions/types.py server/db/models.py server/db/migrations/versions/0021_session_pause_reason.py server/sessions/orchestrator.py server/api/sessions.py server/tests/test_usage_stall_wire.py
printf 'feat(sessions): persist paused_reason/paused_until and emit usageStalled\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t2-msg.txt
git commit -q -F /tmp/t2-msg.txt
```

---

### Task 3: Write, carry and clear the pause reason in the orchestrator

**Files:**
- Modify: `server/sessions/orchestrator.py:9332-9345` (`_pause_session`), `:2410-2517` (`_persist_status`), `:9092-9104` (`_rotation_monitor` pinned branch), `:9845-9851` (`_reassign_locked` account UPDATE)
- Test: `server/tests/test_pause_reason_persist.py`

**Interfaces:**
- Consumes: `SessionRecord.paused_reason/paused_until`, `is_usage_stalled` (Task 2).
- Produces: `_pause_session(record, *, reason, earliest_reset=None)` sets both fields then persists; the WS `status` frame carries `pausedReason`, `pausedUntil`, `usageStalled`; `_persist_status` clears both on any status outside `{"needs_attention", "limited"}`; `_reassign_locked` clears both on success.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pause_reason_persist.py`:

```python
"""_pause_session / _persist_status / _reassign_locked keep paused_reason + paused_until
in sync between the record, the row and the WS status frame."""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

import pytest
from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _row(sid="s1"):
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalars().first()


def test_pause_session_persists_reason_and_reset(tmp_path):
    reset = _dt.datetime(2026, 9, 4, 16, 10, tzinfo=_dt.timezone.utc)

    async def run():
        orch = await _build_orch(tmp_path)
        frames = []
        orch._publish_event = frames.append
        await orch._pause_session(orch.record("s1"), reason="all_accounts_busy",
                                  earliest_reset=reset)
        return orch.record("s1"), await _row(), frames
    rec, row, frames = asyncio.run(run())

    assert rec.status == "needs_attention"
    assert rec.paused_reason == "all_accounts_busy"
    assert rec.paused_until == _dt.datetime(2026, 9, 4, 16, 10)   # naive UTC
    assert row.paused_reason == "all_accounts_busy"
    assert row.paused_until == _dt.datetime(2026, 9, 4, 16, 10)
    status_frames = [f for f in frames if f.get("type") == "status"]
    assert status_frames[-1]["pausedReason"] == "all_accounts_busy"
    assert status_frames[-1]["pausedUntil"] == "2026-09-04T16:10:00+00:00"
    assert status_frames[-1]["usageStalled"] is True


def test_pause_session_without_reset_leaves_until_null(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._publish_event = lambda e: None
        await orch._pause_session(orch.record("s1"), reason="crash_cap")
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.paused_reason == "crash_cap" and rec.paused_until is None
    assert row.paused_reason == "crash_cap" and row.paused_until is None


def test_persist_status_clears_reason_on_non_stalled_status(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="needs_attention",
                                 paused_reason="all_accounts_busy",
                                 paused_until=_dt.datetime(2026, 9, 4, 16, 10))
        frames = []
        orch._publish_event = frames.append
        assert orch.record("s1").paused_reason == "all_accounts_busy"   # rehydrated
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1"), await _row(), frames
    rec, row, frames = asyncio.run(run())
    assert rec.paused_reason is None and rec.paused_until is None
    assert row.paused_reason is None and row.paused_until is None
    assert frames[-1]["usageStalled"] is False
    assert frames[-1]["pausedReason"] is None


def test_persist_status_keeps_reason_while_still_stalled(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="needs_attention",
                                 paused_reason="rotation_cap")
        orch._publish_event = lambda e: None
        await orch._persist_status("s1", "needs_attention")
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.paused_reason == "rotation_cap"
    assert row.paused_reason == "rotation_cap"


# --- _reassign_locked clears the fields on success (reload_session-style harness) ----

class _NullCtx:
    def __init__(self, sink): self.sink = sink
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt, *a, **k):
        self.sink.append(stmt)
        return None


class _NewDriver:
    def __init__(self, *a, **k):
        self.cwd = "/tmp"; self.mode = "interactive"; self.identity = k.get("identity")
        self.claude_session_id = "c1"; self.sent = []
    async def start(self): pass
    async def send_prompt(self, p): self.sent.append(p)
    async def set_model(self, m): pass


class _OldDriver:
    cwd = "/tmp"; mode = "interactive"; identity = SYSTEM_IDENTITY; claude_session_id = "c1"
    async def interrupt(self): pass
    async def aclose(self): pass


@pytest.mark.asyncio
async def test_reassign_clears_pause_fields(monkeypatch):
    stmts = []
    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx(stmts))
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", status="needs_attention",
                        paused_reason="all_accounts_busy",
                        paused_until=_dt.datetime(2026, 9, 4, 16, 10))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {"s1": rec}; orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {"s1": "go"}; orch._turn_restart_at = {}; orch._swapping = {}
    orch._clock = time.time; orch._registry = None; orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_claude_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    await orch._reassign(rec, "b", reason="manual_rotate")

    assert rec.account == "b"
    assert rec.paused_reason is None and rec.paused_until is None
    # The account UPDATE also nulls the two columns.
    compiled = str(stmts[-1].compile(compile_kwargs={"literal_binds": True}))
    assert "paused_reason" in compiled and "paused_until" in compiled
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_pause_reason_persist.py -q`
Expected: failures on `paused_reason` assertions (attributes stay `None`) and the missing frame keys.

- [ ] **Step 3: `_pause_session` sets the fields before persisting**

Replace the body of `_pause_session` (`orchestrator.py:9332-9345`) with:

```python
    async def _pause_session(self, record: SessionRecord, *, reason: str,
                             earliest_reset=None) -> None:
        """No recovery possible (no healthy account / rotation cap). Flag the session
        for the user and publish a machine-readable paused event. The reason and the
        earliest known reset ride the same _persist_status write as the status so the
        dashboard can say WHY (usage_stall.py) instead of a bare "Turn failed"."""
        record.paused_reason = reason
        record.paused_until = _naive_utc(earliest_reset)
        try:
            await self._persist_status(record.id, "needs_attention")
        except Exception:
            log.warning("pause.persist_status failed session=%s", record.id, exc_info=True)
        self._publish_event({
            "type": "paused", "sessionId": record.id, "account": record.account,
            "reason": reason,
            "earliestReset": iso_utc(earliest_reset),
        })
        log.warning("recover.paused session=%s reason=%s", record.id, reason)
```

Add a module-level helper next to the other small helpers near the top of `orchestrator.py` (after `_rethread_identity`, line ~215):

```python
def _naive_utc(dt):
    """Normalize an optional datetime to naive UTC for the DateTime columns (the
    selector's earliest_reset is tz-aware; _persist_status writes naive UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        from datetime import timezone
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
```

- [ ] **Step 4: `_persist_status` carries or clears the fields**

In `_persist_status`, inside the `if rec is not None:` block (after the `interrupted_by_restart` clearing at line ~2477) add:

```python
            # Stalled-on-usage bookkeeping (usage_stall.py): a status outside the two
            # parked shapes means the session is moving again, so the cause is stale.
            if status not in _STALLED_STATUSES:
                rec.paused_reason = None
                rec.paused_until = None
```

In the DB write, after `values: dict = {"status": status}` add:

```python
            if status in _STALLED_STATUSES:
                if rec is not None:
                    values["paused_reason"] = rec.paused_reason
                    values["paused_until"] = rec.paused_until
            else:
                values["paused_reason"] = None
                values["paused_until"] = None
```

In the published `status` frame add three keys:

```python
                             "pausedReason": rec.paused_reason if rec is not None else None,
                             "pausedUntil": iso_utc(rec.paused_until) if rec is not None else None,
                             "usageStalled": (is_usage_stalled(status, rec.paused_reason)
                                              if rec is not None else False),
```

Add near the other status-set constants (line ~96-122):

```python
# The two parked shapes whose cause paused_reason explains (usage_stall.py). Any other
# status clears it.
_STALLED_STATUSES = frozenset({"needs_attention", "limited"})
```

and `from server.sessions.usage_stall import is_usage_stalled` with the other `server.sessions` imports.

- [ ] **Step 5: The pinned branch of `_rotation_monitor` records its cause**

In the `if record.account_pinned and usage is not None:` branch (line ~9092), before `record.status = "limited"` add:

```python
                        record.paused_reason = "pinned_account_limited"
                        record.paused_until = _naive_utc(
                            getattr(usage, "session_resets_at", None)
                            if getattr(usage, "session_pct", 0.0) >= _ROTATION_CEILING
                            else getattr(usage, "weekly_resets_at", None))
```

- [ ] **Step 6: `_reassign_locked` clears on success**

In `_reassign_locked`, change the account UPDATE (`.values(account_name=new_account)`) to:

```python
                    .values(account_name=new_account, paused_reason=None, paused_until=None)
```

and immediately before that `async with db_session() as db:` block add:

```python
            # A successful swap ends whatever pause parked the session; the replayed
            # turn's own status persist may not arrive (no prompt to replay), so clear
            # the cause here too.
            record.paused_reason = None
            record.paused_until = None
```

- [ ] **Step 7: Run the tests**

Run: `$PYT server/tests/test_pause_reason_persist.py server/tests/test_persist_pr_resolution.py server/tests/test_reload_session.py server/tests/test_reassign_reapplies_model.py server/tests/test_restart_turn_interruption.py server/tests/test_orchestrator_recovery.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pause_reason_persist.py
printf 'feat(orchestrator): pause reason + earliest reset ride _persist_status; cleared on move\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t3-msg.txt
git commit -q -F /tmp/t3-msg.txt
```

---

### Task 4: Weekly ceiling counts as dead

**Files:**
- Modify: `server/sessions/orchestrator.py:9053-9057` (`_rotation_monitor`), `:9123-9133` (`_account_is_dead`)
- Test: `server/tests/test_dead_account_rotation.py`

**Interfaces:**
- Consumes: `ccswitch.selector.at_ceiling` (Task 1).
- Produces: the monitor rotates on `weekly_pct >= _ROTATION_CEILING` with reason `weekly limit reached`; `_account_is_dead` is True on either dimension.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_dead_account_rotation.py` (helpers `_make_orch`, `_rec`, `_poller_with_failures`, `_run_monitor_one_event`, `_orch_with_poller` already exist there):

```python
@pytest.mark.asyncio
async def test_rotation_monitor_rotates_at_weekly_ceiling():
    """weekly_pct=100 with session headroom is just as dead — the next turn 429s."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    usage = Usage(session_pct=0.0, session_resets_at=None,
                  weekly_pct=100.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert ("rotate", "s1", "weekly limit reached") in o.calls


def test_account_is_dead_on_weekly_ceiling():
    usage = Usage(session_pct=10.0, session_resets_at=None,
                  weekly_pct=100.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    assert o._account_is_dead("copilot_acct") is True


def test_account_is_alive_below_both_ceilings():
    usage = Usage(session_pct=99.0, session_resets_at=None,
                  weekly_pct=99.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    assert o._account_is_dead("copilot_acct") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_dead_account_rotation.py -q -k "weekly or alive"`
Expected: 2 failures (`weekly limit reached` not in calls; `_account_is_dead` False).

- [ ] **Step 3: Implement**

In `_rotation_monitor`, replace

```python
                    session_pct = getattr(usage, "session_pct", 0.0)
                    if session_pct < _ROTATION_CEILING:
                        continue
                    reason = "session limit reached"
```

with

```python
                    if not at_ceiling(usage, _ROTATION_CEILING):
                        continue
                    session_pct = getattr(usage, "session_pct", 0.0)
                    reason = ("session limit reached" if session_pct >= _ROTATION_CEILING
                              else "weekly limit reached")
```

In `_account_is_dead`, replace `return getattr(usage, "session_pct", 0.0) >= _ROTATION_CEILING` with `return at_ceiling(usage, _ROTATION_CEILING)`.

Add `from ccswitch.selector import at_ceiling` beside the existing `ccswitch` imports in `orchestrator.py`.

- [ ] **Step 4: Run the tests**

Run: `$PYT server/tests/test_dead_account_rotation.py server/tests/test_orchestrator_recovery.py server/tests/test_recovery.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_dead_account_rotation.py
printf 'fix(rotation): weekly ceiling counts as a dead account (monitor + crash recovery)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t4-msg.txt
git commit -q -F /tmp/t4-msg.txt
```

---

### Task 5: Sweep covers `limited` sessions and fires on account recovery

**Files:**
- Modify: `server/sessions/orchestrator.py:9165-9184` (`_sweep_needs_attention`), `:9053-9056` (`_rotation_monitor` below-ceiling branch), `:255-270` (constants)
- Test: `server/tests/test_stuck_session_sweep.py`, `server/tests/test_dead_account_rotation.py`

**Interfaces:**
- Consumes: `at_ceiling`, `is_usage_stalled`, `self._poller.cached(account)`.
- Produces: `_maybe_sweep_on_recovery() -> bool` (debounced by `_RECOVERY_SWEEP_DEBOUNCE_S = 30.0`); `_sweep_needs_attention` reassigns a `limited` record on its own account with reason `limit_reset` once that account is below ceiling.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_stuck_session_sweep.py`:

```python
import types
from ccswitch.oauth import Usage


def _usage(session, weekly):
    return Usage(session_pct=session, session_resets_at=None,
                 weekly_pct=weekly, weekly_resets_at=None, extra_usage_enabled=False)


def _with_poller(o, cached):
    o._poller = types.SimpleNamespace(cached=lambda account: cached)

    async def spy_reassign(record, new_account, *, reason, replay_last_prompt=True):
        o.calls.append(("reassign", record.id, new_account, reason))

    o._reassign = spy_reassign
    return o


@pytest.mark.asyncio
async def test_sweep_resumes_limited_session_once_its_account_recovers():
    o = _with_poller(_make_orch(), _usage(20.0, 30.0))
    rec = _rec(o, account="pinned_acct", status="limited")
    rec.account_pinned = True

    await o._sweep_needs_attention()
    await asyncio.sleep(0)

    assert ("reassign", "s1", "pinned_acct", "limit_reset") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)   # the pin is honoured


@pytest.mark.asyncio
async def test_sweep_leaves_limited_session_while_account_still_at_ceiling():
    o = _with_poller(_make_orch(), _usage(100.0, 30.0))
    _rec(o, account="pinned_acct", status="limited")

    await o._sweep_needs_attention()
    await asyncio.sleep(0)

    assert o.calls == []


@pytest.mark.asyncio
async def test_sweep_leaves_limited_session_with_no_cached_usage():
    o = _with_poller(_make_orch(), None)
    _rec(o, account="pinned_acct", status="limited")

    await o._sweep_needs_attention()
    await asyncio.sleep(0)

    assert o.calls == []


# --- recovery-triggered sweep ---------------------------------------------------

def test_maybe_sweep_on_recovery_fires_once_per_debounce_window():
    o = _make_orch()
    rec = _rec(o, status="needs_attention")
    rec.paused_reason = "all_accounts_busy"
    swept = []

    async def spy_sweep():
        swept.append(True)

    o._sweep_needs_attention = spy_sweep
    o._clock = lambda: 1000.0
    assert o._maybe_sweep_on_recovery() is True
    assert o._maybe_sweep_on_recovery() is False          # debounced
    o._clock = lambda: 1000.0 + 31.0
    assert o._maybe_sweep_on_recovery() is True


def test_maybe_sweep_on_recovery_skips_when_nothing_is_stalled():
    o = _make_orch()
    rec = _rec(o, status="needs_attention")
    rec.paused_reason = "crash_cap"                       # not a usage stall
    _rec(o, "s2", status="idle")
    assert o._maybe_sweep_on_recovery() is False
```

Append to `server/tests/test_dead_account_rotation.py`:

```python
@pytest.mark.asyncio
async def test_rotation_monitor_triggers_recovery_sweep_below_ceiling():
    """An account dropping below ceiling is the recovery signal the periodic sweep
    otherwise waits up to 300 s for."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    fired = []
    o._maybe_sweep_on_recovery = lambda: fired.append(True) or True
    usage = Usage(session_pct=20.0, session_resets_at=None,
                  weekly_pct=20.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert fired == [True]
    assert not any(c[0] in ("rotate", "restart_driverless") for c in o.calls)
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_stuck_session_sweep.py server/tests/test_dead_account_rotation.py -q -k "limited or recovery"`
Expected: failures (`AttributeError: _maybe_sweep_on_recovery`; limited sessions untouched).

- [ ] **Step 3: Implement the sweep branch**

In `_sweep_needs_attention`, replace the `if record.status != "needs_attention": continue` guard with:

```python
            if record.status == "limited":
                # Pinned at its ceiling. The pin means "stay here", so once THIS account
                # has headroom again resume the conversation on it (replays the failed
                # prompt) — never rotate. No cached usage / still at ceiling → wait.
                if not self._owns_record(record, enabled_hosts):
                    continue
                usage = self._poller.cached(record.account)
                if usage is None or at_ceiling(usage, _ROTATION_CEILING):
                    continue
                log.info("stuck-sweep.limit_reset session=%s account=%s",
                         record.id, record.account)
                self._spawn(
                    self._reassign(record, record.account, reason="limit_reset"),
                    name=f"sweep-limit-reset-{record.id}",
                )
                continue
            if record.status != "needs_attention":
                continue
```

- [ ] **Step 4: Implement the recovery trigger**

Add the constant after `_STUCK_SESSION_SWEEP_INTERVAL`:

```python
# Minimum spacing between recovery-triggered sweeps (an account dropping below ceiling
# in the poller stream). The poller emits one event per account per ~60 s; without a
# debounce a fleet of healthy accounts would re-run the sweep every few seconds.
_RECOVERY_SWEEP_DEBOUNCE_S: float = 30.0
```

Add the method right after `_sweep_needs_attention`:

```python
    def _maybe_sweep_on_recovery(self) -> bool:
        """Poller saw an account BELOW ceiling. If any session is stalled on usage,
        run the stuck-session sweep now instead of waiting for its 300 s tick.
        Debounced; returns True when a sweep was spawned."""
        now = self._clock()
        last = getattr(self, "_last_recovery_sweep_at", float("-inf"))
        if now - last < _RECOVERY_SWEEP_DEBOUNCE_S:
            return False
        if not any(is_usage_stalled(r.status, r.paused_reason)
                   for r in list(self._records.values())):
            return False
        self._last_recovery_sweep_at = now
        log.info("stuck-sweep.recovery_trigger")
        self._spawn(self._sweep_needs_attention(), name="recovery-sweep")
        return True
```

In `_rotation_monitor`, change the below-ceiling branch from Task 4 to:

```python
                    if not at_ceiling(usage, _ROTATION_CEILING):
                        self._maybe_sweep_on_recovery()
                        continue
```

- [ ] **Step 5: Run the tests**

Run: `$PYT server/tests/test_stuck_session_sweep.py server/tests/test_dead_account_rotation.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_stuck_session_sweep.py server/tests/test_dead_account_rotation.py
printf 'feat(sweep): resume limited sessions on reset; sweep immediately when an account recovers\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t5-msg.txt
git commit -q -F /tmp/t5-msg.txt
```

---

### Task 6: `rotate_to_best` and `set_account_pin` primitives with fleet plumbing

**Files:**
- Modify: `server/sessions/orchestrator.py` (after `reload_session`, line ~7593; constant near `_REPLAY_STATUSES` line ~105)
- Modify: `server/sessions/facade.py:149`, `server/sessions/remote.py:580-584`, `server/sessions/fleet_orchestrator.py:79-80`, `server/ipc/orchestrator_rpc.py:277`
- Test: `server/tests/test_rotate_to_best.py`, `server/tests/test_account_pin.py`

**Interfaces:**
- Consumes: `select_account`, `AllAccountsBusy`, `_reassign`, `_rotate_away`, `_pause_session`, `_persist_pin`, `_rethread_identity`, `is_usage_stalled`.
- Produces: `rotate_to_best(session_id, identity=SYSTEM_IDENTITY) -> "rotated" | "skipped:<reason>" | "no_driver"`; `set_account_pin(session_id, pinned: bool) -> "pinned" | "unpinned" | "skipped:not-found"`; `_ROTATE_REPLAY_STATUSES = _REPLAY_STATUSES | {"needs_attention", "limited"}`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_rotate_to_best.py`:

```python
"""rotate_to_best — the "Next best account" action and the batch Rotate-stalled button.

A thin caller of _rotate_away's selection + _reassign (the single swap primitive):
picks the next best account via the selector, un-pins, replays the failed prompt for a
stalled session, waits (no replay) for a session parked on a question, and never counts
toward the automatic rotation cap.
"""
import datetime as _dt

import pytest

from ccswitch.selector import AllAccountsBusy, Picked
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator, _ROTATE_REPLAY_STATUSES, _REPLAY_STATUSES
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY


class _Driver:
    def __init__(self): self.identity = SYSTEM_IDENTITY


def _rec(status="needs_attention", **kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
                cols=80, rows=24, cwd="/tmp", status=status)
    base.update(kw)
    return SessionRecord(**base)


def _orch(monkeypatch, rec, *, pick="b", with_driver=True):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {rec.id: _Driver()} if with_driver else {}
    o._clock = lambda: 1000.0
    o.live_session_load = lambda: {}
    o.calls = []
    rotations = []
    o._recovery = type("R", (), {
        "quarantined": lambda self, now: frozenset({"q"}),
        "record_rotation": lambda self, sid, now: rotations.append(sid),
    })()
    o.rotations = rotations

    async def _reassign(record, new, *, reason, replay_last_prompt=True):
        o.calls.append(("reassign", new, reason, replay_last_prompt))
        record.account = new
        if with_driver:
            o._drivers[record.id] = _Driver()

    async def _persist_pin(sid, pinned): o.calls.append(("pin", pinned))

    async def _pause_session(record, *, reason, earliest_reset=None):
        o.calls.append(("pause", reason, earliest_reset))

    o._reassign = _reassign
    o._persist_pin = _persist_pin
    o._pause_session = _pause_session
    seen = {}

    def select_account(exclude=frozenset(), load=None, **k):
        seen["exclude"] = exclude
        if isinstance(pick, Exception):
            raise pick
        return Picked(pick)

    monkeypatch.setattr(orch_mod, "select_account", select_account)
    o.seen = seen
    return o


def test_replay_statuses_add_the_two_parked_shapes():
    assert _ROTATE_REPLAY_STATUSES == _REPLAY_STATUSES | {"needs_attention", "limited"}


@pytest.mark.asyncio
async def test_rotates_stalled_session_replays_and_unpins(monkeypatch):
    rec = _rec(status="needs_attention", paused_reason="all_accounts_busy", account_pinned=True)
    o = _orch(monkeypatch, rec)
    assert await o.rotate_to_best("s1", "user@x") == "rotated"
    assert ("reassign", "b", "manual_rotate", True) in o.calls
    assert ("pin", False) in o.calls and rec.account_pinned is False
    assert o.seen["exclude"] == frozenset({"q", "a"})      # quarantined + current
    assert o.rotations == []                                # never counts toward the cap
    assert o._drivers["s1"].identity == "user@x"


@pytest.mark.asyncio
async def test_parked_on_question_resumes_without_replay(monkeypatch):
    o = _orch(monkeypatch, _rec(status="waiting_question"))
    assert await o.rotate_to_best("s1") == "rotated"
    assert ("reassign", "b", "manual_rotate", False) in o.calls


@pytest.mark.asyncio
async def test_mid_turn_session_replays(monkeypatch):
    o = _orch(monkeypatch, _rec(status="in_progress"))
    await o.rotate_to_best("s1")
    assert ("reassign", "b", "manual_rotate", True) in o.calls


@pytest.mark.asyncio
async def test_unknown_and_not_rotatable_skips(monkeypatch):
    o = _orch(monkeypatch, _rec(status="archived"))
    assert await o.rotate_to_best("nope") == "skipped:not-found"
    assert await o.rotate_to_best("s1") == "skipped:archived"
    assert o.calls == []


@pytest.mark.asyncio
async def test_all_busy_on_stalled_session_refreshes_pause(monkeypatch):
    reset = _dt.datetime(2026, 9, 4, 16, 10, tzinfo=_dt.timezone.utc)
    rec = _rec(status="needs_attention", paused_reason="rotation_cap", account_pinned=True)
    o = _orch(monkeypatch, rec, pick=AllAccountsBusy(reset))
    assert await o.rotate_to_best("s1") == "skipped:all_accounts_busy"
    assert ("pause", "all_accounts_busy", reset) in o.calls
    assert ("pin", False) in o.calls                         # the user asked to move
    assert not any(c[0] == "reassign" for c in o.calls)


@pytest.mark.asyncio
async def test_all_busy_on_healthy_session_touches_nothing(monkeypatch):
    rec = _rec(status="idle", account_pinned=True)
    o = _orch(monkeypatch, rec, pick=AllAccountsBusy(None))
    assert await o.rotate_to_best("s1") == "skipped:all_accounts_busy"
    assert o.calls == [] and rec.account_pinned is True


@pytest.mark.asyncio
async def test_no_driver_after_failed_rebuild(monkeypatch):
    o = _orch(monkeypatch, _rec(status="needs_attention"), with_driver=False)
    assert await o.rotate_to_best("s1") == "no_driver"
```

Create `server/tests/test_account_pin.py`:

```python
"""set_account_pin — the unpin button. Unpinning a `limited` session rotates it now."""
import asyncio

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch(rec):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o.calls = []

    async def _persist_pin(sid, pinned): o.calls.append(("persist", pinned))

    async def _rotate_away(record, *, reason): o.calls.append(("rotate", reason))

    o._persist_pin = _persist_pin
    o._rotate_away = _rotate_away
    o._bg_tasks = set()
    o._spawn = lambda coro, name=None: o._bg_tasks.add(asyncio.get_running_loop().create_task(coro))
    return o


def _rec(status="idle", pinned=True):
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                         cols=80, rows=24, cwd="/tmp", status=status, account_pinned=pinned)


@pytest.mark.asyncio
async def test_unpin_persists_and_rotates_a_limited_session():
    rec = _rec(status="limited")
    o = _orch(rec)
    assert await o.set_account_pin("s1", False) == "unpinned"
    await asyncio.gather(*o._bg_tasks)
    assert rec.account_pinned is False
    assert ("persist", False) in o.calls
    assert ("rotate", "unpinned") in o.calls


@pytest.mark.asyncio
async def test_unpin_idle_session_does_not_rotate():
    rec = _rec(status="idle")
    o = _orch(rec)
    assert await o.set_account_pin("s1", False) == "unpinned"
    assert not any(c[0] == "rotate" for c in o.calls)


@pytest.mark.asyncio
async def test_pin_persists_current_account():
    rec = _rec(status="idle", pinned=False)
    o = _orch(rec)
    assert await o.set_account_pin("s1", True) == "pinned"
    assert rec.account_pinned is True and ("persist", True) in o.calls


@pytest.mark.asyncio
async def test_unknown_session():
    o = _orch(_rec())
    assert await o.set_account_pin("zz", False) == "skipped:not-found"
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_rotate_to_best.py server/tests/test_account_pin.py -q`
Expected: `ImportError` on `_ROTATE_REPLAY_STATUSES` / `AttributeError: rotate_to_best`.

- [ ] **Step 3: Implement**

Near `_REPLAY_STATUSES` (line ~105) add:

```python
# rotate_to_best replays the last prompt for these. The two parked shapes are the
# whole point of the action — a stalled session's last prompt is the one that failed.
_ROTATE_REPLAY_STATUSES = _REPLAY_STATUSES | {"needs_attention", "limited"}
```

After `reload_session` add:

```python
    async def rotate_to_best(self, session_id: str,
                             identity: str = SYSTEM_IDENTITY) -> str:
        """User-initiated "move to the next best account" — the per-session menu
        item and the batch "Rotate stalled" button.

        Selection is the same as automatic rotation (quarantined accounts and the
        current one excluded, load-aware), the swap is the same ``_reassign``
        primitive, and the differences are deliberate:

        * it un-pins: an explicit "move me" overrides an earlier explicit "stay";
        * it does NOT count toward the rotation cap — that cap guards automatic
          thrash, and a human asking to move is not thrash;
        * the last prompt is replayed for mid-turn AND parked (needs_attention /
          limited) sessions — a stalled session's last prompt is the one that
          failed. A session parked on a question / permission / review resumes
          and waits, the reload rule.

        Returns "rotated" | "skipped:<reason>" | "no_driver".
        """
        record = self._records.get(session_id)
        if record is None:
            return "skipped:not-found"
        if record.status in _NOT_RELOADABLE_STATUSES:
            return f"skipped:{record.status}"
        now = self._clock()
        exclude = self._recovery.quarantined(now=now) | frozenset({record.account})
        try:
            new_account = select_account(exclude=exclude, load=self.live_session_load()).name
        except AllAccountsBusy as e:
            if is_usage_stalled(record.status, record.paused_reason):
                # Already parked: honour the move request (drop the pin) and refresh
                # the earliest-reset the card shows. A healthy session is left as is.
                if record.account_pinned:
                    record.account_pinned = False
                    await self._persist_pin(record.id, False)
                await self._pause_session(record, reason="all_accounts_busy",
                                          earliest_reset=e.earliest_reset)
            return "skipped:all_accounts_busy"
        if record.account_pinned:
            record.account_pinned = False
            await self._persist_pin(record.id, False)
        replay = record.status in _ROTATE_REPLAY_STATUSES
        await self._reassign(record, new_account, reason="manual_rotate",
                             replay_last_prompt=replay)
        driver = self._drivers.get(session_id)
        if driver is None:
            return "no_driver"
        _rethread_identity(driver, identity)
        log_event(log, logging.INFO, "session.rotate_to_best", session_id=session_id,
                  identity=identity, to_account=new_account, replayed=replay)
        return "rotated"

    async def set_account_pin(self, session_id: str, pinned: bool) -> str:
        """Pin the session to its CURRENT account (usage-ceiling rotation then marks
        it `limited` instead of moving it) or clear the pin. Clearing the pin on a
        `limited` session is the recovery the pin was blocking, so it rotates now.
        Returns "pinned" | "unpinned" | "skipped:not-found"."""
        record = self._records.get(session_id)
        if record is None:
            return "skipped:not-found"
        record.account_pinned = pinned
        await self._persist_pin(session_id, pinned)
        if not pinned and record.status == "limited":
            self._spawn(self._rotate_away(record, reason="unpinned"),
                        name=f"unpin-rotate-{session_id}")
        return "pinned" if pinned else "unpinned"
```

- [ ] **Step 4: Fleet plumbing**

`server/sessions/facade.py`, after the `reload_session` line:

```python
    async def rotate_to_best(self, *a, **k): return await _maybe_await(self._orch.rotate_to_best(*a, **k))
    async def set_account_pin(self, *a, **k): return await _maybe_await(self._orch.set_account_pin(*a, **k))
```

`server/sessions/remote.py`, after `reload_session`:

```python
    async def rotate_to_best(self, session_id: str, identity: str = SYSTEM_IDENTITY) -> str:
        """Proxy the owning daemon's rotate_to_best (move to the next best account).
        Returns "rotated" / "skipped:<reason>" / "no_driver"."""
        return await self._client.call(
            "rotate_to_best", session_id=session_id, identity=identity)

    async def set_account_pin(self, session_id: str, pinned: bool) -> str:
        """Proxy the owning daemon's set_account_pin. Returns "pinned" / "unpinned" /
        "skipped:not-found"."""
        return await self._client.call(
            "set_account_pin", session_id=session_id, pinned=pinned)
```

`server/sessions/fleet_orchestrator.py` `_PER_SESSION`: add `"rotate_to_best", "set_account_pin",` after `"reload_session",`.

`server/ipc/orchestrator_rpc.py` `_SIMPLE`: add `"rotate_to_best", "set_account_pin",` after `"reload_session",`.

- [ ] **Step 5: Run the tests**

Run: `$PYT server/tests/test_rotate_to_best.py server/tests/test_account_pin.py server/tests/test_reload_session.py server/tests/test_manual_switch_and_pin.py server/tests/test_fleet_orchestrator.py server/tests/test_orchestrator_rpc.py -q` (if the last two file names differ, run `ls server/tests | grep -i -E 'fleet_orch|orchestrator_rpc'` and substitute).
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/sessions/facade.py server/sessions/remote.py server/sessions/fleet_orchestrator.py server/ipc/orchestrator_rpc.py server/tests/test_rotate_to_best.py server/tests/test_account_pin.py
printf 'feat(orchestrator): rotate_to_best + set_account_pin primitives with fleet plumbing\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t6-msg.txt
git commit -q -F /tmp/t6-msg.txt
```

---

### Task 7: HTTP routes

**Files:**
- Modify: `server/api/sessions.py` (after `batch_resume_sessions` line ~951; after `reload_session` line ~1317)
- Test: `server/tests/test_sessions_rotate_routes.py`

**Interfaces:**
- Consumes: `orch.rotate_to_best(session_id, identity)`, `orch.set_account_pin(session_id, pinned)`, `BatchSessionIdsRequest`, `record_audit`, `_record_to_dict`, `_resolve_repo`.
- Produces: `POST /api/sessions/batch/rotate` → `{succeeded, failed}`; `POST /api/sessions/{id}/rotate` → session dict (404 / 409 `{code}`); `POST /api/sessions/{id}/account-pin` `{pinned}` → session dict (404).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_sessions_rotate_routes.py` (harness copied from `test_sessions_batch_resume.py`):

```python
"""POST /api/sessions/batch/rotate, /{id}/rotate, /{id}/account-pin."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": ""}


def _rec(sid="s1", **kw):
    base = dict(id=sid, name="n", account="a", claude_session_id="c", cols=80, rows=24,
                cwd="/tmp", status="needs_attention")
    base.update(kw)
    return SessionRecord(**base)


class _FakeSessions:
    def __init__(self):
        self.rotate_map: dict[str, str] = {}
        self.records: dict[str, SessionRecord] = {"s1": _rec()}
        self.calls: list[tuple] = []

    async def rotate_to_best(self, sid, identity="system"):
        self.calls.append(("rotate", sid, identity))
        return self.rotate_map.get(sid, "skipped:not-found")

    async def set_account_pin(self, sid, pinned):
        self.calls.append(("pin", sid, pinned))
        rec = self.records.get(sid)
        if rec is None:
            return "skipped:not-found"
        rec.account_pinned = pinned
        return "pinned" if pinned else "unpinned"

    async def get_session(self, sid):
        return self.records.get(sid)

    async def prs_for(self, sid): return []
    async def latest_suggestions(self, sid): return []
    async def latest_summary(self, sid): return ""


def _make_client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


@pytest.fixture
def cf():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        yield client, fake


def test_batch_rotate_partitions_by_outcome(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "rotated", "s2": "skipped:all_accounts_busy"}
    body = client.post("/api/sessions/batch/rotate", json={"ids": ["s1", "s2", "s1"]}, headers=_H).json()
    assert body["succeeded"] == ["s1"]
    assert body["failed"] == [{"id": "s2", "error": "skipped:all_accounts_busy"}]
    assert fake.calls.count(("rotate", "s1", "testuser@example.com")) == 1


def test_batch_rotate_empty_ids_422(cf):
    client, _ = cf
    assert client.post("/api/sessions/batch/rotate", json={"ids": []}, headers=_H).status_code == 422


def test_rotate_one_returns_record(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "rotated"}
    r = client.post("/api/sessions/s1/rotate", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "s1"


def test_rotate_one_409_on_skip(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "skipped:all_accounts_busy"}
    r = client.post("/api/sessions/s1/rotate", headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "skipped:all_accounts_busy"


def test_rotate_one_404_unknown(cf):
    client, _ = cf
    assert client.post("/api/sessions/zz/rotate", headers=_H).status_code == 404


def test_account_pin_unpins(cf):
    client, fake = cf
    fake.records["s1"].account_pinned = True
    r = client.post("/api/sessions/s1/account-pin", json={"pinned": False}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["accountPinned"] is False
    assert ("pin", "s1", False) in fake.calls


def test_account_pin_404_unknown(cf):
    client, _ = cf
    assert client.post("/api/sessions/zz/account-pin", json={"pinned": True}, headers=_H).status_code == 404


def test_routes_require_identity():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        assert client.post("/api/sessions/batch/rotate", json={"ids": ["s1"]}).status_code == 401
        assert client.post("/api/sessions/s1/rotate").status_code == 401
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PYT server/tests/test_sessions_rotate_routes.py -q`
Expected: 404/405 responses where 200/409/422 are expected.

- [ ] **Step 3: Implement**

After `batch_resume_sessions` add:

```python
@router.post("/batch/rotate")
async def batch_rotate_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Move each session to the next best account and continue its conversation —
    the "Rotate stalled (N)" button. Best-effort: "rotated" lands in `succeeded`;
    any skip / no_driver lands in `failed` with the reason as `error`."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            result = await orch.rotate_to_best(session_id, identity)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch rotate failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if result == "rotated":
            succeeded.append(session_id)
            await record_audit(db=db, type="session.rotate", identity=identity,
                               detail={"session_id": session_id, "batch": True})
        else:
            failed.append({"id": session_id, "error": result})
    return {"succeeded": succeeded, "failed": failed}
```

After `reload_session` add:

```python
@router.post("/{session_id}/rotate")
async def rotate_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """"Next best account": move the session to the account the selector ranks
    best and continue the conversation (orch.rotate_to_best — the same driver-swap
    primitive rotation uses). 404 unknown; 409 (code = the orchestrator's reason)
    when nothing has headroom or the session is parked where a move makes no sense."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    result = await orch.rotate_to_best(session_id, identity)
    if result != "rotated":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": result, "message": f"Session could not be rotated: {result}"},
        )
    await record_audit(db, "session.rotate", identity, {"sessionId": session_id})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


class AccountPinRequest(BaseModel):
    pinned: bool


@router.post("/{session_id}/account-pin")
async def set_account_pin(
    session_id: str,
    body: AccountPinRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Pin the session to its current account, or clear the pin (the 📌 button).
    Unpinning a `limited` session rotates it immediately."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.set_account_pin(session_id, body.pinned)
    await record_audit(db, "session.account_pin", identity,
                       {"sessionId": session_id, "pinned": body.pinned})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))
```

Check `BaseModel` is imported in `server/api/sessions.py` (`grep -n '^from pydantic' server/api/sessions.py`); if not, add `from pydantic import BaseModel`.

- [ ] **Step 4: Run the tests**

Run: `$PYT server/tests/test_sessions_rotate_routes.py server/tests/test_sessions_batch_resume.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_rotate_routes.py
printf 'feat(api): POST /sessions/{id}/rotate, /batch/rotate, /{id}/account-pin\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t7-msg.txt
git commit -q -F /tmp/t7-msg.txt
```

---

### Task 8: Web types, status pill, and WS patches

**Files:**
- Modify: `web/src/api/types.ts:130-212` (`Session`), `web/src/session/statusTier.ts`, `web/src/session/triageTier.ts:28-31`, `web/src/components/UsageBadge.tsx:150-175` (`StatusPill`), `web/src/ws/client.ts:20,455-466,532-560`
- Test: `web/src/session/statusTier.test.ts`, `web/src/ws/client.reconcile.test.ts`

**Interfaces:**
- Produces: `Session.pausedReason?: string | null`, `Session.pausedUntil?: string | null`, `Session.usageStalled?: boolean`; `StatusPresentationOpts.usageStalled?`, `.pausedUntil?`; `WsStatusMsg` carries the three fields.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/statusTier.test.ts`:

```ts
describe('usage-stalled presentation', () => {
  it('relabels a usage-paused needs_attention session as waiting for capacity', () => {
    const p = statusPresentation('needs_attention', { usageStalled: true, pausedUntil: '2026-09-04T16:10:00+00:00' })
    expect(p.tier).toBe('waiting')
    expect(p.label).toMatch(/^Waiting for capacity · retries /)
  })
  it('omits the time suffix when no reset is known', () => {
    expect(statusPresentation('needs_attention', { usageStalled: true, pausedUntil: null }).label).toBe('Waiting for capacity')
  })
  it('labels a pinned limited session and names the reset', () => {
    const p = statusPresentation('limited', { usageStalled: true, pausedUntil: '2026-09-04T16:10:00+00:00' })
    expect(p.tier).toBe('waiting')
    expect(p.label).toMatch(/^Usage limit \(pinned\) · resets /)
  })
  it('leaves a plain needs_attention (crash cap) as Turn failed', () => {
    expect(statusPresentation('needs_attention', { usageStalled: false }).label).toBe('Turn failed')
    expect(statusPresentation('needs_attention').tier).toBe('error')
  })
  it('never hijacks a live status even if the flag lingers', () => {
    expect(statusPresentation('in_progress', { usageStalled: true }).label).toBe('Working…')
  })
  it('restart interruption wins over the stall relabel', () => {
    expect(statusPresentation('needs_attention', { usageStalled: true, interruptedByRestart: true }).label).toMatch(/re-send/i)
  })
})
```

Append to `web/src/ws/client.reconcile.test.ts`:

```ts
describe('SessionSocket stall fields', () => {
  it('applies pausedReason/pausedUntil/usageStalled from a status frame', () => {
    const { s, qc } = makeSocket(); seed(qc, { revAt: T1 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'needs_attention', revAt: T2,
      pausedReason: 'all_accounts_busy', pausedUntil: '2026-09-04T16:10:00+00:00', usageStalled: true })
    expect(row(qc).usageStalled).toBe(true)
    expect(row(qc).pausedReason).toBe('all_accounts_busy')
    expect(row(qc).pausedUntil).toBe('2026-09-04T16:10:00+00:00')
  })
  it('a rotated frame clears the stall', () => {
    const { s, qc } = makeSocket()
    seed(qc, { revAt: T1, status: 'needs_attention', usageStalled: true, pausedReason: 'rotation_cap', pausedUntil: T2 })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'a', toAccount: 'b', revAt: T3 })
    expect(row(qc).accountName).toBe('b')
    expect(row(qc).usageStalled).toBe(false)
    expect(row(qc).pausedReason).toBeNull()
    expect(row(qc).pausedUntil).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/session/statusTier.test.ts src/ws/client.reconcile.test.ts)`
Expected: the new cases fail (labels are `Turn failed` / `Usage limit`; fields undefined).

- [ ] **Step 3: Types**

In `web/src/api/types.ts` `Session`, after `interruptedFromStatus` add:

```ts
  /** Why a needs_attention/limited session is parked (server pause reason token,
   *  e.g. "all_accounts_busy", "rotation_cap", "pinned_account_limited"). */
  pausedReason?: string | null
  /** ISO instant of the earliest known usage reset for a parked session, or null. */
  pausedUntil?: string | null
  /** Server-derived: parked because no account has usage headroom (or pinned at its
   *  ceiling). Counts toward the "Rotate stalled (N)" button; relabels the pill. */
  usageStalled?: boolean
```

- [ ] **Step 4: Status pill**

In `web/src/session/statusTier.ts`:

```ts
import { formatResetTime } from '@/lib/formatReset'

// Parked on usage (server usageStalled): auto-recoverable, so it reads as calm
// waiting rather than a red failure — but keeps the amber text so it is findable.
const USAGE_STALLED_STATUSES = new Set(['needs_attention', 'limited'])

function usageStalledPresentation(state: string, pausedUntil?: string | null): StatusPresentation {
  const when = formatResetTime(pausedUntil)
  if (state === 'limited') {
    return { tier: 'waiting', label: when ? `Usage limit (pinned) · resets ${when}` : 'Usage limit (pinned)', Icon: Hourglass, textClass: NEEDS_YOU }
  }
  return { tier: 'waiting', label: when ? `Waiting for capacity · retries ${when}` : 'Waiting for capacity', Icon: Hourglass, textClass: NEEDS_YOU }
}
```

Extend `StatusPresentationOpts`:

```ts
  /** Server-derived Session.usageStalled — parked for lack of usage headroom. */
  usageStalled?: boolean
  /** Session.pausedUntil — earliest known reset, for the pill's time suffix. */
  pausedUntil?: string | null
```

In `statusPresentation`, after the `interruptedByRestart` check and before the table lookup:

```ts
  if (opts?.usageStalled && USAGE_STALLED_STATUSES.has(state)) {
    return usageStalledPresentation(state, opts.pausedUntil)
  }
```

In `web/src/session/triageTier.ts` `sessionTriage`, add to the opts object:

```ts
    usageStalled: session.usageStalled,
    pausedUntil: session.pausedUntil,
```

In `web/src/components/UsageBadge.tsx` `StatusPillProps` add `usageStalled?: boolean` and `pausedUntil?: string | null`, destructure them, and pass `{ interruptedByRestart, usageStalled, pausedUntil }` to `statusPresentation`. Then `grep -rn '<StatusPill' web/src --include=*.tsx | grep -v test` — at every call site that has a `Session` in scope, thread `usageStalled={session.usageStalled} pausedUntil={session.pausedUntil}`.

- [ ] **Step 5: WS client**

In `web/src/ws/client.ts`:

```ts
export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string; updatedAt?: string; revAt?: string; interruptedByRestart?: boolean; pausedReason?: string | null; pausedUntil?: string | null; usageStalled?: boolean }
```

Change the `case 'status'` call to pass the message: `this._patchSessionStatus(msg.sessionId, msg.status as Session['status'], msg.updatedAt, msg.revAt, msg.interruptedByRestart, msg)`. Extend the signature with `stall?: Pick<WsStatusMsg, 'pausedReason' | 'pausedUntil' | 'usageStalled'>` and add to the returned object:

```ts
                 // Stall fields ride the same frame so the "Waiting for capacity" pill
                 // and the Rotate-stalled count update without waiting for the poll.
                 ...(stall?.usageStalled !== undefined
                     ? { usageStalled: stall.usageStalled, pausedReason: stall.pausedReason ?? null, pausedUntil: stall.pausedUntil ?? null }
                     : {}) }
```

In `_patchSessionAccount`, change the return to
`return { ...s, accountName, usageStalled: false, pausedReason: null, pausedUntil: null, ...(revAt ? { revAt } : {}) }`
with a one-line comment: a rotation ends whatever pause parked the session.

- [ ] **Step 6: Run the tests and the type check**

Run: `(cd web && npx vitest run src/session src/ws src/components/__tests__/SessionCard.test.tsx && npx tsc -b && npx eslint src/session/statusTier.ts src/session/triageTier.ts src/ws/client.ts src/components/UsageBadge.tsx src/api/types.ts)`
Expected: all pass, no type or lint errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/session/statusTier.ts web/src/session/triageTier.ts web/src/components/UsageBadge.tsx web/src/ws/client.ts web/src/session/statusTier.test.ts web/src/ws/client.reconcile.test.ts
printf 'feat(web): "Waiting for capacity" pill from usageStalled; stall fields on the status frame\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t8-msg.txt
git commit -q -F /tmp/t8-msg.txt
```

---

### Task 9: Hooks and the "Rotate stalled (N)" toolbar button

**Files:**
- Modify: `web/src/api/hooks.ts` (after `useReloadSession` ~345 and after `useResumeInterrupted` ~722), `web/src/components/SessionList.tsx:150,216-219,308-319`
- Test: `web/src/components/__tests__/SessionList.test.tsx`

**Interfaces:**
- Produces: `useRotateStalled(): UseMutationResult<BatchResult, ApiRequestError, string[]>`; `useRotateSession(): UseMutationResult<Session, ApiRequestError, { id: string }>`; `useSetAccountPin(): UseMutationResult<Session, ApiRequestError, { id: string; pinned: boolean }>`.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/components/__tests__/SessionList.test.tsx` (reuse `SESSIONS`, `ACCOUNTS`, `wrapper`, `session`):

```ts
describe('SessionList — Rotate stalled button', () => {
  const sharedProps = {
    accounts: ACCOUNTS, onNew: vi.fn(), onOpen: vi.fn(), onRename: vi.fn(),
    onClose: vi.fn(), onDelete: vi.fn(), onQuickResume: vi.fn(),
  }
  beforeEach(() => vi.clearAllMocks())

  it('renders "Rotate stalled (2)" when two sessions are usageStalled', () => {
    const sessions: Session[] = [
      { ...SESSIONS[0], status: 'needs_attention', usageStalled: true },
      { ...SESSIONS[1], status: 'limited', usageStalled: true },
      session({ id: 'sess-3', name: 'Healthy', accountName: 'alice', status: 'idle',
                createdAt: '2026-06-19T12:00:00Z', updatedAt: '2026-06-19T12:00:00Z' }),
    ]
    render(<SessionList {...sharedProps} sessions={sessions} />, { wrapper })
    expect(screen.getByRole('button', { name: /rotate stalled \(2\)/i })).toBeInTheDocument()
  })

  it('does not render the button when nothing is stalled', () => {
    render(<SessionList {...sharedProps} sessions={SESSIONS} />, { wrapper })
    expect(screen.queryByRole('button', { name: /rotate stalled/i })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/components/__tests__/SessionList.test.tsx -t "Rotate stalled")`
Expected: the first case fails (no button).

- [ ] **Step 3: Hooks**

In `web/src/api/hooks.ts` after `useReloadSession`:

```ts
// "Next best account": the daemon moves the session to the account the selector
// ranks best and continues the conversation (replays a stalled prompt). The caller
// re-attaches the socket on success, exactly as reload does.
export function useRotateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.post<Session>(`/sessions/${id}/rotate`, {}),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
    onError: (e) => { toast.error(e.body && typeof e.body === 'object' && 'detail' in e.body ? 'No account has headroom right now' : `Couldn't rotate session`) },
  })
}

// Pin the session to its current account (usage-ceiling rotation then parks it as
// `limited` instead of moving it) or clear the pin; unpinning a limited session
// rotates it immediately on the server.
export function useSetAccountPin() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; pinned: boolean }>({
    mutationFn: ({ id, pinned }) => api.post<Session>(`/sessions/${id}/account-pin`, { pinned }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

(Check how `ApiRequestError` exposes the body — `grep -n 'class ApiRequestError' -A8 web/src/api/client.ts` — and adapt the `onError` field access; if there is no body accessor, use the plain `Couldn't rotate session` toast.)

After `useResumeInterrupted`:

```ts
export function useRotateStalled() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/rotate', { ids }),
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Rotated ${n} · ${f} skipped`)
      else toast.success(`Rotated ${n}`)
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

- [ ] **Step 4: Button**

In `web/src/components/SessionList.tsx`: import `Shuffle` from `lucide-react` and `useRotateStalled` from `@/api/hooks`; next to `const resumeInterrupted = useResumeInterrupted()` add `const rotateStalled = useRotateStalled()`; after `handleResumeInterrupted` add:

```tsx
  const stalledIds = sessions.filter((s) => s.usageStalled).map((s) => s.id)
  const handleRotateStalled = () => {
    if (stalledIds.length > 0) rotateStalled.mutate(stalledIds)
  }
```

and after the Resume-interrupted button block:

```tsx
        {!selectMode && stalledIds.length > 0 && (
          <button
            type="button"
            onClick={handleRotateStalled}
            disabled={rotateStalled.isPending}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          >
            <Shuffle size={12} aria-hidden="true" />
            Rotate stalled ({stalledIds.length})
          </button>
        )}
```

- [ ] **Step 5: Run the FULL web suite and fix manual hook mocks**

Run: `(cd web && npx vitest run 2>&1 | tee /tmp/t9-vitest.log | tail -30)`
Any test that mocks `@/api/hooks` with a factory and renders `SessionList` now fails with `useRotateStalled is not a function`. For each failing file (`grep -l "useResumeInterrupted" web/src/**/*.test.tsx` is the shortlist) add `useRotateStalled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),` beside its `useResumeInterrupted` mock entry. Re-run until green. Then `(cd web && npx tsc -b && npx eslint src/api/hooks.ts src/components/SessionList.tsx)`.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/hooks.ts web/src/components/SessionList.tsx web/src/components/__tests__/SessionList.test.tsx $(git diff --name-only | grep test)
printf 'feat(web): "Rotate stalled (N)" batch button + rotate/pin hooks\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t9-msg.txt
git commit -q -F /tmp/t9-msg.txt
```

---

### Task 10: "Next best account" menu item and the unpin button

**Files:**
- Modify: `web/src/components/session/SessionActions.tsx`, `web/src/features/sessions/SessionHeader.tsx:55-80,146-153`, `web/src/features/sessions/MainPaneSession.tsx:60-108`, `web/src/components/session/SessionMeta.tsx:57-62,106-108`, `web/src/components/session/SessionBody.tsx:27-38,76-97`
- Test: `web/src/components/session/SessionActions.test.tsx`, `web/src/components/session/SessionMeta.test.tsx`

**Interfaces:**
- Consumes: `useRotateSession`, `useSetAccountPin` (Task 9), `rt.reattach()`.
- Produces: `SessionActions` prop `onRotate?: () => void`; `SessionHeader` prop `onRotate?: () => void`; `SessionMeta` prop `onUnpin?: () => void`.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/components/session/SessionActions.test.tsx`:

```ts
describe('SessionActions — Next best account', () => {
  it('renders the item only when onRotate is given, and calls it', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useCloseSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onRotate = vi.fn()

    const { rerender } = render(<SessionActions sessionId="s1" sessionName="My Session" onDone={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.queryByRole('menuitem', { name: /next best account/i })).not.toBeInTheDocument()

    rerender(<SessionActions sessionId="s1" sessionName="My Session" onDone={vi.fn()} onRotate={onRotate} />)
    await user.click(screen.getByRole('menuitem', { name: /next best account/i }))
    expect(onRotate).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })
})
```

In `web/src/components/session/SessionMeta.test.tsx`, replace the `shows a pin indicator when pinned` case with:

```ts
  it('renders the pin as an Unpin button and dispatches onUnpin', () => {
    const onUnpin = vi.fn()
    render(<SessionMeta {...base} accountPinned onSelectModel={vi.fn()} onSelectAccount={vi.fn()} onUnpin={onUnpin} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unpin account' }))
    expect(onUnpin).toHaveBeenCalledTimes(1)
  })

  it('renders no pin control when not pinned', () => {
    render(<SessionMeta {...base} onSelectModel={vi.fn()} onSelectAccount={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Unpin account' })).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/components/session/SessionActions.test.tsx src/components/session/SessionMeta.test.tsx)`
Expected: the new cases fail.

- [ ] **Step 3: SessionActions + SessionHeader + MainPaneSession**

`SessionActions.tsx`: add `Shuffle` to the lucide import; add the prop

```ts
  /**
   * Surface a "Next best account" menu item that calls this — move the session to
   * the account the selector ranks best and continue the conversation. The host owns
   * the mutation + socket re-attach, like `onReload`. Omitted → no item.
   */
  onRotate?: () => void
```

destructure it, and render after the Reload item (same classes):

```tsx
            {onRotate && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onRotate() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-gray-700 dark:text-gray-200',
                  'hover:bg-gray-100 dark:hover:bg-gray-800',
                  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
                )}
              >
                <Shuffle size={16} aria-hidden="true" />
                Next best account
              </button>
            )}
```

`SessionHeader.tsx`: add `/** "Next best account" menu item — the host rotates the account and re-attaches. */ onRotate?: () => void` to the props, destructure it, and pass `onRotate={onRotate}` to `<SessionActions>` beside `onReload`.

`MainPaneSession.tsx`: import `useRotateSession` with `useReloadSession`; add

```ts
  const rotateSession = useRotateSession()
  // "Next best account": same shape as reload — the daemon swaps the driver, then
  // this client re-attaches so the transcript reflects the daemon's state.
  function rotate() {
    rotateSession.mutate({ id: sessionId }, { onSuccess: () => rt.reattach() })
  }
```

and pass `onRotate={rotate}` to `<SessionHeader>`.

- [ ] **Step 4: SessionMeta + SessionBody**

`SessionMeta.tsx`: add `onUnpin?: () => void` to `Props` (doc: "Clear the account pin; the server rotates a `limited` session immediately."), destructure it, and replace the pinned `<span>` with:

```tsx
          {accountPinned && (
            <button
              type="button"
              onClick={onUnpin}
              aria-label="Unpin account"
              title="Pinned — auto-rotation off. Click to unpin."
              className="text-amber-500 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
            >
              📌
            </button>
          )}
```

`SessionBody.tsx`: import `useSetAccountPin` with the other hooks; add `const setAccountPin = useSetAccountPin()`; pass `onUnpin={() => setAccountPin.mutate({ id: rt.sessionId, pinned: false })}` to `<SessionMeta>`.

- [ ] **Step 5: Run the FULL web suite, fix manual mocks, type-check, lint**

Run: `(cd web && npx vitest run 2>&1 | tee /tmp/t10-vitest.log | tail -30)`
Tests that mock `@/api/hooks` and render `SessionBody` / `MainPaneSession` now need `useSetAccountPin` / `useRotateSession` entries: add `useSetAccountPin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),` and `useRotateSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),` beside their `useReloadSession` / `useModelApproval` mocks. Re-run until green. Then `(cd web && npx tsc -b && npx eslint src)`.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/SessionActions.tsx web/src/features/sessions/SessionHeader.tsx web/src/features/sessions/MainPaneSession.tsx web/src/components/session/SessionMeta.tsx web/src/components/session/SessionBody.tsx web/src/components/session/SessionActions.test.tsx web/src/components/session/SessionMeta.test.tsx $(git diff --name-only | grep test)
printf 'feat(web): "Next best account" menu item and click-to-unpin\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/t10-msg.txt
git commit -q -F /tmp/t10-msg.txt
```

---

### Task 11: Full gates, memory note, PR

**Files:**
- No new code. Fix whatever the gates surface.

- [ ] **Step 1: Server gates**

```bash
/home/tony/GitHub/ccswitch/.venv/bin/ruff check src/ server/ 2>&1 | tail -5
/home/tony/GitHub/ccswitch/.venv/bin/mypy server/ --ignore-missing-imports 2>&1 | tail -5
$PYT tests/ -q 2>&1 | tail -3
$PYT server/tests/ -q -x --timeout=300 2>&1 | tee /tmp/t11-server-tests.log | tail -5
```

Run the server suite in the background if it exceeds the Bash timeout (`run_in_background`, then read the log). A single `dispose_db` teardown hang (300 s timeout) is a known flake; re-run that file alone.

- [ ] **Step 2: Web gates**

```bash
(cd web && npx vitest run 2>&1 | tail -5 && npx tsc -b && npx eslint src && echo WEB-OK)
```

- [ ] **Step 3: Push and open the PR**

Check for in-flight PRs touching the same files first:
`gh pr list --repo steiner385/cc-account-switcher --search "orchestrator.py SessionList" --state open --limit 10`.

```bash
git push -u origin feat/usage-stalled-rotate
cat > /tmp/pr-body.md <<'EOF'
## Summary

Sessions stalled because their account spent its usage now:

- show **why** — `paused_reason` / `paused_until` are persisted and ride the WS status frame; a usage-paused card reads "Waiting for capacity · retries 12:10" (waiting tier) instead of a red "Turn failed"; a pinned session at its ceiling reads "Usage limit (pinned) · resets …"
- have a **shortcut** — `Rotate stalled (N)` beside "Resume interrupted" (`POST /sessions/batch/rotate`), a `Next best account` session-menu item (`POST /sessions/{id}/rotate`), and a click-to-unpin 📌 (`POST /sessions/{id}/account-pin`)
- **recover on their own more often** — the weekly ceiling counts as a dead account (monitor + crash recovery), a `limited` session resumes on its own account when that account's window resets, and the stuck-session sweep fires as soon as the poller sees an account drop below ceiling (30 s debounce) instead of waiting for its 300 s tick

`rotate_to_best` is a thin caller of the existing `_reassign` swap primitive: un-pins, picks via the selector, replays the failed prompt for parked sessions, never counts toward the automatic rotation cap.

Spec: `docs/superpowers/specs/2026-09-04-usage-stalled-rotation-design.md`. PR 2 (model-aware selection from the per-model `weekly_scoped` limits) follows.

## Evidence

rocky journal 2026-09-04 10:21–10:38 EDT: five sessions paused `all_accounts_busy` once a minute, then `rotation_cap`, parked as "Turn failed" until a daemon restart.

## Test plan

- [ ] `ruff`, `mypy`, `pytest tests/`, `pytest server/tests/` green
- [ ] `vitest run`, `tsc -b`, `eslint` green
- [ ] Manual: a session on an at-ceiling account shows the waiting pill with the reset time; `Rotate stalled` moves it and the conversation continues

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --repo steiner385/cc-account-switcher --title "feat(sessions): rotate usage-stalled sessions to the next best account — shortcut, visible pause reason, weekly ceiling" --body-file /tmp/pr-body.md
gh pr merge --repo steiner385/cc-account-switcher --squash --auto <PR#>
```

- [ ] **Step 4: Verify the PR is ready (not draft) with auto-merge armed**

`gh pr view <PR#> --repo steiner385/cc-account-switcher --json isDraft,autoMergeRequest -q '{draft: .isDraft, auto: .autoMergeRequest.enabledAt}'` → `draft: false`, `auto` non-null.
