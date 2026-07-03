# Automation Tab Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Automation tab framework — a registry + contract every automation plugs into (on/off + mode, live status, persisted capped activity feed, schema-driven config, durable per-target state, de-duplicated session spawning + provenance) — and migrate the existing pr_autofix onto it as the reference automation `pr-doctor`.

**Architecture:** Automations are singletons defined in code (`server/automations/`); the DB stores only their state (`automation` row), activity (`automation_event`), and per-target state (`automation_target_state`). An `AutomationContext` is the sole outward seam (emit / target-state / spawn_session), so logging, per-target state, and spawn de-dup can't be re-implemented per automation. A new `/api/automations` router feeds an `AutomationView` tab of expandable cards. The pr_autofix migration is the final task: it re-points the orchestrator's mode read + loop-guard onto the registry and deletes `AppSettings.pr_autofix_mode` in the same change.

**Tech Stack:** Python 3 · FastAPI · SQLAlchemy 2.x async (aiosqlite dev / Postgres prod) · React + Vite + TypeScript · TanStack Query · Tailwind/shadcn · React Router · pytest · vitest + tsc + eslint.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-30-automation-tab-shell-design.md` is the source of truth.
- **Nullable-only DDL on dev SQLite:** new columns MUST be nullable (`init_db`'s `_reconcile_sqlite_columns` cannot add NOT NULL columns; `create_all` never ALTERs). New *tables* are created by `create_all`. Prod/Postgres gets a real Alembic migration for the 3 tables + 2 columns.
- **No parallel state (constitution VI):** `grep -rn pr_autofix_mode server/ web/` MUST return 0 hits after Task 9.
- **Vocabulary single source:** `pr-doctor.modes` MUST be sourced from `server.sessions.pr_fix.VALID_AUTOFIX_MODES`, never a re-typed literal.
- **Auth:** every `/api/*` route depends on `current_identity`. Tests send header `{"Tailscale-User-Login": "testuser@example.com"}` and run with `CCSWITCH_DEV=1` + empty `CCSWITCH_TAILNET_ALLOWLIST`.
- **Event feed cap:** 500 rows per automation, pruned on insert (best-effort; a prune failure never blocks the emit).
- **`emit` is best-effort:** it swallows+logs all exceptions and never propagates into the automation's control flow.
- **`status()` must be cheap:** derive from per-target state / row data; never a live GitHub call.
- **Commit cadence:** one commit per task (after its tests pass). Server tests: `pytest server/tests/`. Web tests (run in `web/`): `vitest run`, `tsc --noEmit`, `eslint`.
- **Test env note:** a fresh worktree has no built env — run server tests via the main checkout's prebuilt venv (`<main-checkout>/.venv/bin/python -m pytest …`) and symlink `web/node_modules` from the main checkout.

---

## File Structure

**New backend package `server/automations/`:**
- `__init__.py` — exports the registry singleton + public types.
- `base.py` — `Automation` Protocol, `AutomationStatus`, `ConfigField`/`ConfigSchema`, `validate_config()` (pure, no I/O).
- `context.py` — `AutomationContext` (emit + prune, get/set_target_state, spawn_session).
- `registry.py` — `AutomationRegistry` (register singletons, startup upsert, lookup).
- `pr_doctor.py` — the `pr-doctor` reference automation definition.

**New API:** `server/api/automations.py` — the `/api/automations` router.

**Modified backend:**
- `server/db/models.py` — add `Automation`, `AutomationEvent`, `AutomationTargetState`; add `Session.origin`/`origin_ref`; **remove** `AppSettings.pr_autofix_mode`.
- `server/sessions/orchestrator.py` — `create_session` gains `origin`/`origin_ref`; `_pr_autofix_mode` reads the registry; `_on_pr_change` uses `ctx` target-state + emits; construct the registry + pr-doctor's `ctx`.
- `server/app.py` — import + `include_router(automations_router)`; start the registry in `_lifespan`.
- `server/api/settings.py` — remove `prAutofixMode` from body/`_to_dict`/PUT.

**New frontend:**
- `web/src/api/automations.ts` — typed API client.
- `web/src/components/AutomationView.tsx` — the tab body (expandable cards).
- `web/src/components/AutomationCard.tsx` — one automation card (collapsed + expanded).

**Modified frontend:**
- `web/src/api/hooks.ts` — add automation hooks + types; **remove** `prAutofixMode` from `AppSettings`.
- `web/src/components/Dashboard.tsx` — `'automation'` in `TabId`/`TAB_ORDER`/`tabRefs`, tab button, render `AutomationView`.
- `web/src/App.tsx` — `/automation` route.
- `web/src/components/settings/SettingsPanel.tsx` — remove the PR auto-fix dropdown.
- `web/src/components/SessionCard.tsx` — "Created by {name}" provenance badge when `origin` set.

**Dependency order:** Task 1 (models) → 2 (base) → 3 (context: emit/state) → 4 (context: spawn) → 5 (registry) → 6 (pr-doctor def) → 7 (API) → 8 (wiring) → 9 (migration). Frontend 10 (client/hooks) → 11 (card/view) → 12 (tab/route/badge/settings-cleanup). Frontend tasks 10–12 depend only on the API shape from Tasks 7–8.

---

### Task 1: Database models — automation tables + session provenance

**Files:**
- Modify: `server/db/models.py` (imports line 11; add 3 model classes; add 2 `Session` columns; remove `AppSettings.pr_autofix_mode` line 188-190 — **deferred to Task 9** so the migration lands atomically. In THIS task, ADD only.)
- Test: `server/tests/test_automation_models.py` (create)

**Interfaces:**
- Produces: ORM models `Automation(id, enabled, mode, config, last_active_at, updated_at)`, `AutomationEvent(id, automation_id, ts, level, kind, summary, target, session_id, detail)`, `AutomationTargetState(automation_id, target, state, updated_at)`; `Session.origin`, `Session.origin_ref` (both `str | None`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automation_models.py
"""Automation tables + Session provenance columns create and round-trip."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from sqlalchemy import select


@pytest.fixture
async def db():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from server.db import init_db, db_session
        await init_db("sqlite+aiosqlite:///:memory:")
        async with db_session() as session:
            yield session


async def test_automation_row_round_trips(db):
    from server.db.models import Automation
    db.add(Automation(id="pr-doctor", enabled=True, mode="auto", config={"repos": ["a/b"]}))
    await db.flush()
    row = (await db.execute(select(Automation).where(Automation.id == "pr-doctor"))).scalar_one()
    assert row.enabled is True
    assert row.mode == "auto"
    assert row.config == {"repos": ["a/b"]}
    assert row.last_active_at is None


async def test_event_and_target_state_round_trip(db):
    from server.db.models import Automation, AutomationEvent, AutomationTargetState
    db.add(Automation(id="pr-doctor"))
    await db.flush()
    db.add(AutomationEvent(automation_id="pr-doctor", level="info", kind="detected",
                           summary="CI fail", target="a/b#1", detail={"x": 1}))
    db.add(AutomationTargetState(automation_id="pr-doctor", target="a/b#1", state={"attempts": 1}))
    await db.flush()
    ev = (await db.execute(select(AutomationEvent))).scalar_one()
    assert ev.kind == "detected" and ev.target == "a/b#1" and ev.session_id is None
    ts = (await db.execute(select(AutomationTargetState))).scalar_one()
    assert ts.state == {"attempts": 1}


async def test_session_has_origin_columns(db):
    from server.db.models import Session
    s = Session(id="s1", name="n", account_name="acct", cwd="/tmp",
                origin="pr-doctor", origin_ref="a/b#1")
    db.add(s)
    await db.flush()
    row = (await db.execute(select(Session).where(Session.id == "s1"))).scalar_one()
    assert row.origin == "pr-doctor" and row.origin_ref == "a/b#1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automation_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Automation'`.

- [ ] **Step 3: Add the models and columns**

In `server/db/models.py`, change the import on line 11 to add `ForeignKey`:

```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
```

Add to the `Session` class (after `account_pinned`, before `created_at`):

```python
    # Provenance: the automation id that spawned this session (NULL = human-created),
    # and what it was spawned for (e.g. "owner/name#412"). Nullable so init_db backfills
    # on the dev SQLite DB without a migration. See server/automations/.
    origin: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    origin_ref: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

Append three new model classes at the end of the file:

```python
class Automation(Base):
    """Registry state for one code-defined automation. Identity (name, schema,
    behavior) lives in server/automations/; this row stores only mutable state."""

    __tablename__ = "automation"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # stable slug
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Heartbeat the automation bumps while alive; lets the UI flag a stalled loop.
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AutomationEvent(Base):
    """One row in an automation's append-only, capped activity feed."""

    __tablename__ = "automation_event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    automation_id: Mapped[str] = mapped_column(
        String, ForeignKey("automation.id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, index=True)
    level: Mapped[str] = mapped_column(String, nullable=False, default="info")  # info/warn/error
    # Queryable event code: detected/attempted/recovered/escalated/gave_up/spawned/skipped_duplicate.
    kind: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True, default=None)  # display-only
    session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, default=None)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)


class AutomationTargetState(Base):
    """Durable per-target state (retry counters, loop-guard, 'gave up') keyed by
    (automation_id, target). The automation deletes a row when its target reaches
    terminal rest (e.g. PR merged), so the table does not grow unbounded."""

    __tablename__ = "automation_target_state"

    automation_id: Mapped[str] = mapped_column(
        String, ForeignKey("automation.id"), primary_key=True)
    target: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_automation_models.py -v`
Expected: PASS (3 tests).
> Note: SQLite does not enforce FK `ON DELETE SET NULL` unless `PRAGMA foreign_keys=ON`; the clause is for prod/Postgres. Do not write a test asserting DB-level cascade on SQLite.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_automation_models.py
git commit -m "feat(automation): automation registry tables + session provenance columns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Contract value types + config validation (pure)

**Files:**
- Create: `server/automations/__init__.py` (empty for now)
- Create: `server/automations/base.py`
- Test: `server/tests/test_automation_base.py` (create)

**Interfaces:**
- Produces: `AutomationStatus` (dataclass: `text: str`, `level: str = "idle"`, `counts: dict | None = None`; `.to_dict()`); `ConfigField` (dataclass: `key, label, type, default, help=""`); `ConfigSchema = list[ConfigField]`; `FIELD_TYPES = {"bool","int","string","enum","repo-multiselect"}`; `validate_config(schema, config) -> dict` (returns coerced config, raises `ValueError` on unknown key / wrong type / bad enum); `Automation` Protocol.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automation_base.py
"""Pure config-schema validation + status value type."""
from __future__ import annotations

import pytest


def _schema():
    from server.automations.base import ConfigField
    return [
        ConfigField(key="enabled_repos", label="Repos", type="repo-multiselect", default=[]),
        ConfigField(key="max_attempts", label="Max attempts", type="int", default=3),
        ConfigField(key="lens", label="Lens", type="enum", default="a",
                    help="", choices=["a", "b"]),
    ]


def test_validate_config_accepts_and_coerces():
    from server.automations.base import validate_config
    out = validate_config(_schema(), {"max_attempts": 5, "enabled_repos": ["x/y"]})
    assert out["max_attempts"] == 5
    assert out["enabled_repos"] == ["x/y"]
    assert out["lens"] == "a"  # default filled


def test_validate_config_rejects_unknown_key():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="unknown config key"):
        validate_config(_schema(), {"nope": 1})


def test_validate_config_rejects_wrong_type():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="max_attempts"):
        validate_config(_schema(), {"max_attempts": "five"})


def test_validate_config_rejects_bad_enum():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="lens"):
        validate_config(_schema(), {"lens": "z"})


def test_status_to_dict():
    from server.automations.base import AutomationStatus
    s = AutomationStatus(text="watching 3 PRs", level="active", counts={"failing": 3})
    assert s.to_dict() == {"text": "watching 3 PRs", "level": "active", "counts": {"failing": 3}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automation_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations'`.

- [ ] **Step 3: Write the implementation**

Create `server/automations/__init__.py` empty. Create `server/automations/base.py`:

```python
"""Automation contract value types + pure config validation (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

FIELD_TYPES = {"bool", "int", "string", "enum", "repo-multiselect"}


@dataclass
class ConfigField:
    key: str
    label: str
    type: str
    default: Any
    help: str = ""
    choices: list[str] | None = None  # required for type == "enum"

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": self.default, "help": self.help, "choices": self.choices}


ConfigSchema = list[ConfigField]


@dataclass
class AutomationStatus:
    text: str
    level: str = "idle"  # idle | active | warn | error
    counts: dict | None = None

    def to_dict(self) -> dict:
        return {"text": self.text, "level": self.level, "counts": self.counts}


def _type_ok(field_type: str, value: Any) -> bool:
    if field_type == "bool":
        return isinstance(value, bool)
    if field_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in ("string", "enum"):
        return isinstance(value, str)
    if field_type == "repo-multiselect":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return False


def validate_config(schema: ConfigSchema, config: dict) -> dict:
    """Validate `config` against `schema`. Returns a coerced dict with every
    schema key present (defaults filled). Raises ValueError on an unknown key,
    a wrong type, or a value outside an enum's choices."""
    known = {f.key: f for f in schema}
    for key in config:
        if key not in known:
            raise ValueError(f"unknown config key: {key!r}")
    out: dict = {}
    for f in schema:
        if f.key in config:
            value = config[f.key]
            if not _type_ok(f.type, value):
                raise ValueError(f"{f.key}: expected {f.type}, got {type(value).__name__}")
            if f.type == "enum" and value not in (f.choices or []):
                raise ValueError(f"{f.key}: {value!r} not in {f.choices}")
            out[f.key] = value
        else:
            out[f.key] = f.default
    return out


@runtime_checkable
class Automation(Protocol):
    id: str
    name: str
    description: str
    modes: tuple[str, ...]
    config_schema: ConfigSchema

    def status(self, ctx: "Any") -> AutomationStatus: ...
    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: "Any") -> None: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_automation_base.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/__init__.py server/automations/base.py server/tests/test_automation_base.py
git commit -m "feat(automation): contract value types + pure config validation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: AutomationContext — emit (capped) + per-target state

**Files:**
- Create: `server/automations/context.py`
- Test: `server/tests/test_automation_context.py` (create)

**Interfaces:**
- Consumes: models from Task 1; `db_session` from `server.db`.
- Produces: `AutomationContext(automation_id, *, create_session=None, event_cap=500)` with `async emit(level, summary, *, kind=None, target=None, session_id=None, detail=None)`, `async get_target_state(target) -> dict`, `async set_target_state(target, state) -> None`, `async list_events(*, limit=100, target=None, kind=None) -> list[dict]`. (`spawn_session` added in Task 4.)

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automation_context.py
"""AutomationContext emit (cap + filters) and per-target state."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch


@pytest.fixture
async def ctx():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from server.db import init_db, db_session
        from server.db.models import Automation
        from server.automations.context import AutomationContext
        await init_db("sqlite+aiosqlite:///:memory:")
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        yield AutomationContext("pr-doctor", event_cap=5)


async def test_emit_appends_and_lists_newest_first(ctx):
    await ctx.emit("info", "first", kind="detected", target="a/b#1")
    await ctx.emit("warn", "second", kind="attempted", target="a/b#1")
    events = await ctx.list_events()
    assert [e["summary"] for e in events] == ["second", "first"]
    assert events[0]["kind"] == "attempted"


async def test_emit_prunes_to_cap(ctx):
    for i in range(8):  # cap is 5
        await ctx.emit("info", f"e{i}")
    events = await ctx.list_events(limit=100)
    assert len(events) == 5
    assert events[0]["summary"] == "e7"  # newest kept


async def test_list_events_filters(ctx):
    await ctx.emit("info", "x", kind="detected", target="a/b#1")
    await ctx.emit("info", "y", kind="attempted", target="a/b#2")
    assert [e["summary"] for e in await ctx.list_events(target="a/b#1")] == ["x"]
    assert [e["summary"] for e in await ctx.list_events(kind="attempted")] == ["y"]


async def test_emit_swallows_errors(ctx):
    # A bad session_id FK or any failure must not raise into the caller.
    await ctx.emit("info", "ok")  # baseline works
    with patch("server.automations.context.db_session", side_effect=RuntimeError("boom")):
        await ctx.emit("info", "should not raise")  # no exception


async def test_target_state_round_trip(ctx):
    assert await ctx.get_target_state("a/b#1") == {}
    await ctx.set_target_state("a/b#1", {"attempts": 2})
    assert await ctx.get_target_state("a/b#1") == {"attempts": 2}
    await ctx.set_target_state("a/b#1", {"attempts": 3})
    assert await ctx.get_target_state("a/b#1") == {"attempts": 3}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automation_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.context'`.

- [ ] **Step 3: Write the implementation**

Create `server/automations/context.py`:

```python
"""AutomationContext — the sole outward seam for an automation: activity feed,
per-target state, and (Task 4) de-duplicated session spawning."""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import delete, select

from server.db import db_session
from server.db.models import AutomationEvent, AutomationTargetState

log = logging.getLogger(__name__)


class AutomationContext:
    def __init__(self, automation_id: str, *,
                 create_session: Callable[..., Any] | None = None,
                 event_cap: int = 500) -> None:
        self.automation_id = automation_id
        self._create_session = create_session
        self._event_cap = event_cap

    async def emit(self, level: str, summary: str, *, kind: str | None = None,
                   target: str | None = None, session_id: str | None = None,
                   detail: dict | None = None) -> None:
        """Append one activity event, then prune to the cap. Best-effort:
        swallows + logs all errors; never propagates into the caller."""
        try:
            async with db_session() as db:
                db.add(AutomationEvent(
                    automation_id=self.automation_id, level=level, summary=summary,
                    kind=kind, target=target, session_id=session_id, detail=detail))
                await db.flush()
                await self._prune(db)
        except Exception:
            log.warning("automation emit failed (automation=%s)", self.automation_id,
                        exc_info=True)

    async def _prune(self, db) -> None:
        ids = (await db.execute(
            select(AutomationEvent.id)
            .where(AutomationEvent.automation_id == self.automation_id)
            .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc())
            .offset(self._event_cap)
        )).scalars().all()
        if ids:
            await db.execute(delete(AutomationEvent).where(AutomationEvent.id.in_(ids)))

    async def list_events(self, *, limit: int = 100, target: str | None = None,
                          kind: str | None = None) -> list[dict]:
        stmt = (select(AutomationEvent)
                .where(AutomationEvent.automation_id == self.automation_id)
                .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc()))
        if target is not None:
            stmt = stmt.where(AutomationEvent.target == target)
        if kind is not None:
            stmt = stmt.where(AutomationEvent.kind == kind)
        stmt = stmt.limit(limit)
        async with db_session() as db:
            rows = (await db.execute(stmt)).scalars().all()
        return [{"ts": r.ts.isoformat(), "level": r.level, "kind": r.kind,
                 "summary": r.summary, "target": r.target, "sessionId": r.session_id}
                for r in rows]

    async def get_target_state(self, target: str) -> dict:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            return dict(row.state) if row else {}

    async def set_target_state(self, target: str, state: dict) -> None:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            if row is None:
                db.add(AutomationTargetState(
                    automation_id=self.automation_id, target=target, state=state))
            else:
                row.state = state
            await db.flush()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_automation_context.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/context.py server/tests/test_automation_context.py
git commit -m "feat(automation): AutomationContext emit (capped, filterable) + target state

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: AutomationContext.spawn_session — provenance + de-dup

**Files:**
- Modify: `server/automations/context.py` (add `spawn_session`)
- Modify: `server/sessions/orchestrator.py` (`create_session` accepts + persists `origin`/`origin_ref`)
- Test: `server/tests/test_automation_spawn.py` (create)
- Test: `server/tests/test_orchestrator_origin.py` (create)

**Interfaces:**
- Consumes: `create_session` callable injected into the context; `Session` model.
- Produces: `async spawn_session(self, *, origin_ref, dedupe_key=None, **create_kwargs)` → the created (or existing duplicate) session record. `orchestrator.create_session(..., origin=None, origin_ref=None)` persists those onto the `Session` row.

- [ ] **Step 1: Write the failing test (context-level, injected create_session)**

```python
# server/tests/test_automation_spawn.py
"""spawn_session stamps provenance, auto-emits, and de-dups non-terminal sessions."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch


@pytest.fixture
async def env():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from server.db import init_db, db_session
        from server.db.models import Automation
        await init_db("sqlite+aiosqlite:///:memory:")
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
        yield


async def _fake_create_session(*, origin=None, origin_ref=None, **kw):
    # Mimics orchestrator.create_session: persists a Session row with provenance.
    from server.db import db_session
    from server.db.models import Session
    async with db_session() as db:
        s = Session(id=f"sess-{origin_ref}", name="auto", account_name="a", cwd="/tmp",
                    status="working", origin=origin, origin_ref=origin_ref)
        db.add(s)
    return s


async def test_spawn_stamps_origin_and_emits(env):
    from server.automations.context import AutomationContext
    ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
    rec = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
    assert rec.origin == "pr-doctor" and rec.origin_ref == "a/b#1"
    spawned = [e for e in await ctx.list_events() if e["kind"] == "spawned"]
    assert spawned and spawned[0]["sessionId"] == "sess-a/b#1"


async def test_spawn_dedups_non_terminal(env):
    from server.automations.context import AutomationContext
    calls = []

    async def counting_create(**kw):
        calls.append(kw)
        return await _fake_create_session(**kw)

    ctx = AutomationContext("pr-doctor", create_session=counting_create)
    first = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
    second = await ctx.spawn_session(origin_ref="a/b#1", name="auto")
    assert len(calls) == 1  # second call short-circuited
    assert second.id == first.id
    assert any(e["kind"] == "skipped_duplicate" for e in await ctx.list_events())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automation_spawn.py -v`
Expected: FAIL — `AttributeError: 'AutomationContext' object has no attribute 'spawn_session'`.

- [ ] **Step 3: Add spawn_session to the context**

Append to `AutomationContext` in `server/automations/context.py`:

```python
    async def spawn_session(self, *, origin_ref: str, dedupe_key: str | None = None,
                            **create_kwargs) -> Any:
        """Find-or-create a session for this automation. If a NON-terminal session
        with the same (origin, dedupe_key) already exists, return it and emit a
        'skipped_duplicate' event instead of creating a second. Otherwise create
        via the injected create_session (which stamps origin/origin_ref), then emit
        a linked 'spawned' event."""
        from server.db.models import Session
        key = dedupe_key or origin_ref
        async with db_session() as db:
            existing = (await db.execute(select(Session).where(
                Session.origin == self.automation_id,
                Session.origin_ref == key,
                Session.status != "ended"))).scalars().first()
        if existing is not None:
            await self.emit("info", f"skipped duplicate spawn for {key}",
                            kind="skipped_duplicate", target=key, session_id=existing.id)
            return existing
        if self._create_session is None:
            raise RuntimeError("AutomationContext has no create_session bound")
        rec = await self._create_session(origin=self.automation_id, origin_ref=origin_ref,
                                         **create_kwargs)
        await self.emit("info", f"spawned session for {origin_ref}",
                        kind="spawned", target=origin_ref, session_id=getattr(rec, "id", None))
        return rec
```

- [ ] **Step 4: Run the context test to verify it passes**

Run: `pytest server/tests/test_automation_spawn.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing orchestrator test (real create_session persists provenance)**

```python
# server/tests/test_orchestrator_origin.py
"""orchestrator.create_session persists origin/origin_ref onto the Session row."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from sqlalchemy import select


async def test_create_session_persists_origin():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        from server.db import init_db, db_session
        from server.db.models import Session
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        await init_db("sqlite+aiosqlite:///:memory:")
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        rec = await orch.create_session(name="auto", account="a", cwd="/tmp",
                                        origin="pr-doctor", origin_ref="a/b#1")
        async with db_session() as db:
            row = (await db.execute(select(Session).where(Session.id == rec.id))).scalar_one()
        assert row.origin == "pr-doctor" and row.origin_ref == "a/b#1"
```

> Note: confirm `create_session`'s real keyword names (`name`/`account`/`cwd`) against `orchestrator.py:396` and adjust the call if they differ; the assertion on `origin`/`origin_ref` is what matters.

- [ ] **Step 6: Run it to verify it fails, then thread origin/origin_ref through create_session**

Run: `pytest server/tests/test_orchestrator_origin.py -v` → FAIL (`unexpected keyword argument 'origin'`).
In `server/sessions/orchestrator.py` `create_session` (line 396): add `origin: str | None = None, origin_ref: str | None = None` to the signature, and set them on the `Session(...)` row it persists and on the in-memory `SessionRecord` (mirror an existing nullable field like `repo`). Keep them defaulted so all existing callers are unaffected.

- [ ] **Step 7: Run both spawn tests to verify they pass**

Run: `pytest server/tests/test_automation_spawn.py server/tests/test_orchestrator_origin.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add server/automations/context.py server/sessions/orchestrator.py server/tests/test_automation_spawn.py server/tests/test_orchestrator_origin.py
git commit -m "feat(automation): spawn_session with provenance stamping + de-dup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Registry — register singletons + startup upsert

**Files:**
- Modify: `server/automations/registry.py` (create)
- Modify: `server/automations/__init__.py` (export `registry`)
- Test: `server/tests/test_automation_registry.py` (create)

**Interfaces:**
- Consumes: `Automation` Protocol (Task 2), models (Task 1).
- Produces: `AutomationRegistry` with `register(automation)`, `get(id) -> Automation`, `all() -> list[Automation]`, `async ensure_rows()` (upsert each automation's row, preserving existing state), `context_for(id) -> AutomationContext`. A module-level singleton `registry`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automation_registry.py
"""Registry registration + idempotent startup upsert."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from sqlalchemy import select


class _Fake:
    id = "fake"
    name = "Fake"
    description = "d"
    modes = ("off", "on")
    config_schema: list = []
    def status(self, ctx): 
        from server.automations.base import AutomationStatus
        return AutomationStatus(text="idle")
    async def on_state_change(self, **kw): ...


@pytest.fixture
async def db_env():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from server.db import init_db
        await init_db("sqlite+aiosqlite:///:memory:")
        yield


async def test_register_and_get():
    from server.automations.registry import AutomationRegistry
    r = AutomationRegistry()
    r.register(_Fake())
    assert r.get("fake").name == "Fake"
    assert [a.id for a in r.all()] == ["fake"]


async def test_duplicate_id_rejected():
    from server.automations.registry import AutomationRegistry
    r = AutomationRegistry()
    r.register(_Fake())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_Fake())


async def test_ensure_rows_upserts_then_preserves_state(db_env):
    from server.automations.registry import AutomationRegistry
    from server.db import db_session
    from server.db.models import Automation
    r = AutomationRegistry()
    r.register(_Fake())
    await r.ensure_rows()
    async with db_session() as db:
        row = (await db.execute(select(Automation).where(Automation.id == "fake"))).scalar_one()
        assert row.enabled is False  # default
        row.enabled = True  # simulate a user toggle
    await r.ensure_rows()  # second startup must NOT reset it
    async with db_session() as db:
        row = (await db.execute(select(Automation).where(Automation.id == "fake"))).scalar_one()
        assert row.enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automation_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.registry'`.

- [ ] **Step 3: Write the implementation**

Create `server/automations/registry.py`:

```python
"""Static registry of code-defined automations + startup row upsert."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import Automation
from .context import AutomationContext


class AutomationRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Automation] = {}

    def register(self, automation: Automation) -> None:
        if automation.id in self._by_id:
            raise ValueError(f"automation {automation.id!r} already registered")
        self._by_id[automation.id] = automation

    def get(self, automation_id: str) -> Automation:
        return self._by_id[automation_id]

    def all(self) -> list[Automation]:
        return list(self._by_id.values())

    def context_for(self, automation_id: str, *, create_session=None) -> AutomationContext:
        return AutomationContext(automation_id, create_session=create_session)

    async def ensure_rows(self) -> None:
        """Upsert one row per registered automation. Creates absent rows with
        enabled=False; NEVER overwrites existing state (idempotent across restarts)."""
        async with db_session() as db:
            existing = set((await db.execute(select(AutomationRow.id))).scalars().all())
            for a in self._by_id.values():
                if a.id not in existing:
                    db.add(AutomationRow(id=a.id, enabled=False))
            await db.flush()


registry = AutomationRegistry()
```

Update `server/automations/__init__.py`:

```python
from .registry import registry

__all__ = ["registry"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_automation_registry.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/registry.py server/automations/__init__.py server/tests/test_automation_registry.py
git commit -m "feat(automation): registry with idempotent startup row upsert

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: pr-doctor reference automation definition

**Files:**
- Create: `server/automations/pr_doctor.py`
- Modify: `server/automations/registry.py` (register `PrDoctor` into the module `registry`)
- Test: `server/tests/test_pr_doctor_automation.py` (create)

**Interfaces:**
- Consumes: `VALID_AUTOFIX_MODES` from `server.sessions.pr_fix`; `AutomationStatus`, `ConfigField` from base.
- Produces: `PrDoctor` automation (`id="pr-doctor"`, `name="PR Doctor"`, `modes=tuple(sorted(VALID_AUTOFIX_MODES))` with `off` first, `config_schema=[enabled_repos: repo-multiselect]`, `status(ctx)` derived from target-state counts). Registered at import time.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_doctor_automation.py
"""The pr-doctor reference automation: identity, modes-from-source, status."""
from __future__ import annotations


def test_modes_sourced_from_valid_autofix_modes():
    from server.automations.pr_doctor import PrDoctor
    from server.sessions.pr_fix import VALID_AUTOFIX_MODES
    pd = PrDoctor()
    assert set(pd.modes) == set(VALID_AUTOFIX_MODES)
    assert pd.modes[0] == "off"  # off renders first in the dropdown
    assert pd.id == "pr-doctor" and pd.name == "PR Doctor"


def test_config_schema_has_repo_filter():
    from server.automations.pr_doctor import PrDoctor
    keys = {f.key: f for f in PrDoctor().config_schema}
    assert keys["enabled_repos"].type == "repo-multiselect"


def test_status_is_cheap_and_idle_by_default():
    from server.automations.pr_doctor import PrDoctor
    s = PrDoctor().status(ctx=None)  # must not touch I/O
    assert s.level in ("idle", "active") and isinstance(s.text, str)


def test_registered_in_registry():
    from server.automations.registry import registry
    assert "pr-doctor" in [a.id for a in registry.all()]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_pr_doctor_automation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.pr_doctor'`.

- [ ] **Step 3: Write the implementation**

Create `server/automations/pr_doctor.py`:

```python
"""The pr-doctor reference automation — the registry home for the existing
pr_autofix behavior. 'pr-doctor' is the permanent slug; the richer recovery
behavior (archived sessions, merge conflicts, retries) lands in spec #2 under
this same id. Behavior currently lives in the orchestrator's _on_pr_change
(see Task 9); this class supplies identity, modes, config schema, and status."""
from __future__ import annotations

from server.sessions.pr_fix import VALID_AUTOFIX_MODES

from .base import AutomationStatus, ConfigField


def _ordered_modes() -> tuple[str, ...]:
    # off first (dropdown default-most-conservative), then the rest, stable.
    rest = sorted(m for m in VALID_AUTOFIX_MODES if m != "off")
    return ("off", *rest)


class PrDoctor:
    id = "pr-doctor"
    name = "PR Doctor"
    description = "Watches CI on PRs created via ccswitch and restores failing ones."
    modes = _ordered_modes()
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories",
                    type="repo-multiselect", default=[],
                    help="Limit to these repos (empty = all ccswitch-managed repos)."),
    ]

    def status(self, ctx) -> AutomationStatus:
        # Cheap: the orchestrator bumps a live in-memory summary it injects here in
        # Task 9. With no live data (ctx is None / no counts), report idle.
        counts = getattr(ctx, "pr_doctor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        failing = counts.get("failing", 0)
        return AutomationStatus(
            text=f"watching {failing} failing PR(s)" if failing else "all PRs green",
            level="active" if failing else "idle", counts=counts)
```

Append to `server/automations/registry.py` (after `registry = AutomationRegistry()`):

```python
from .pr_doctor import PrDoctor  # noqa: E402  (register the built-in automations)

registry.register(PrDoctor())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_pr_doctor_automation.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_doctor.py server/automations/registry.py server/tests/test_pr_doctor_automation.py
git commit -m "feat(automation): pr-doctor reference automation (modes from VALID_AUTOFIX_MODES)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `/api/automations` router

**Files:**
- Create: `server/api/automations.py`
- Test: `server/tests/test_automations_api.py` (create)

**Interfaces:**
- Consumes: `registry` (Task 5/6); `AutomationContext` (Task 3); models (Task 1); `current_identity`, `get_db`, `db_session`.
- Produces: router with `GET /api/automations`, `GET /api/automations/{id}/events`, `GET /api/automations/{id}/sessions`, `PATCH /api/automations/{id}`. Exported as `router`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automations_api.py
"""/api/automations: list, events, sessions, PATCH validation, auth."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_list_includes_pr_doctor():
    c = _client()
    r = c.get("/api/automations", headers=H)
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()}
    assert "pr-doctor" in ids
    pd = next(a for a in r.json() if a["id"] == "pr-doctor")
    assert pd["modes"][0] == "off"
    assert "configSchema" in pd and "status" in pd

def test_requires_auth():
    c = _client()
    assert c.get("/api/automations").status_code == 401

def test_patch_valid_mode_persists():
    c = _client()
    r = c.patch("/api/automations/pr-doctor", json={"enabled": True, "mode": "auto"}, headers=H)
    assert r.status_code == 200
    assert c.get("/api/automations", headers=H).json()[0]  # smoke
    pd = next(a for a in c.get("/api/automations", headers=H).json() if a["id"] == "pr-doctor")
    assert pd["enabled"] is True and pd["mode"] == "auto"

def test_patch_bad_mode_422():
    c = _client()
    assert c.patch("/api/automations/pr-doctor", json={"mode": "nope"}, headers=H).status_code == 422

def test_patch_bad_config_422():
    c = _client()
    r = c.patch("/api/automations/pr-doctor", json={"config": {"unknown": 1}}, headers=H)
    assert r.status_code == 422

def test_patch_unknown_automation_404():
    c = _client()
    assert c.patch("/api/automations/nope", json={"enabled": True}, headers=H).status_code == 404

def test_events_endpoint_empty_ok():
    c = _client()
    r = c.get("/api/automations/pr-doctor/events", headers=H)
    assert r.status_code == 200 and r.json() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_automations_api.py -v`
Expected: FAIL — 404s (router not registered yet).

- [ ] **Step 3: Write the router**

Create `server/api/automations.py`:

```python
"""Automation registry API — list/read/configure code-defined automations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from server.auth.middleware import current_identity
from server.automations.base import validate_config
from server.automations.context import AutomationContext
from server.automations.registry import registry
from server.db import get_db
from server.db.models import Automation as AutomationRow, Session as DbSession

router = APIRouter(prefix="/api/automations", tags=["automations"])


class PatchBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    config: dict | None = None


async def _row(db, automation_id: str) -> AutomationRow:
    row = (await db.execute(
        select(AutomationRow).where(AutomationRow.id == automation_id))).scalar_one_or_none()
    if row is None:
        row = AutomationRow(id=automation_id, enabled=False)
        db.add(row)
        await db.flush()
    return row


def _automation_or_404(automation_id: str):
    try:
        return registry.get(automation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown automation")


@router.get("")
async def list_automations(_identity: str = Depends(current_identity), db=Depends(get_db)) -> list[dict]:
    out = []
    for a in registry.all():
        row = await _row(db, a.id)
        out.append({
            "id": a.id, "name": a.name, "description": a.description,
            "enabled": row.enabled, "mode": row.mode, "modes": list(a.modes),
            "status": a.status(ctx=None).to_dict(),
            "configSchema": [f.to_dict() for f in a.config_schema],
            "config": row.config or {},
            "lastActiveAt": row.last_active_at.isoformat() if row.last_active_at else None,
        })
    return out


@router.get("/{automation_id}/events")
async def list_events(automation_id: str, limit: int = 100, target: str | None = None,
                      kind: str | None = None,
                      _identity: str = Depends(current_identity)) -> list[dict]:
    _automation_or_404(automation_id)
    ctx = AutomationContext(automation_id)
    return await ctx.list_events(limit=limit, target=target, kind=kind)


@router.get("/{automation_id}/sessions")
async def spawned_sessions(automation_id: str, _identity: str = Depends(current_identity),
                           db=Depends(get_db)) -> list[dict]:
    _automation_or_404(automation_id)
    rows = (await db.execute(
        select(DbSession).where(DbSession.origin == automation_id)
        .order_by(DbSession.created_at.desc()).limit(50))).scalars().all()
    return [{"id": s.id, "name": s.name, "status": s.status, "originRef": s.origin_ref}
            for s in rows]


@router.patch("/{automation_id}")
async def patch_automation(automation_id: str, body: PatchBody,
                           _identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    automation = _automation_or_404(automation_id)
    row = await _row(db, automation_id)
    if body.mode is not None:
        if body.mode not in automation.modes:
            raise HTTPException(status_code=422, detail=f"invalid mode {body.mode!r}")
        row.mode = body.mode
    if body.config is not None:
        try:
            row.config = validate_config(automation.config_schema, body.config)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.flush()
    # React to the change (best-effort; failure surfaces as an event, not a 500).
    ctx = AutomationContext(automation_id)
    try:
        await automation.on_state_change(enabled=row.enabled, mode=row.mode,
                                         config=row.config or {}, ctx=ctx)
    except Exception:
        await ctx.emit("error", "on_state_change failed", kind="error")
    return {"id": automation_id, "enabled": row.enabled, "mode": row.mode,
            "config": row.config or {}}
```

> Note: `PrDoctor` has no `on_state_change` in Task 6's class — add a no-op `async def on_state_change(self, **kw): ...` to `PrDoctor` now so the PATCH path works; Task 9 fills it in if needed.

- [ ] **Step 4: Register the router (needed for the test to pass)**

In `server/app.py`: add to the import block (~line 36) `from server.api.automations import router as automations_router`, and in the inclusion block (~line 431) `app.include_router(automations_router)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest server/tests/test_automations_api.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add server/api/automations.py server/app.py server/automations/pr_doctor.py server/tests/test_automations_api.py
git commit -m "feat(automation): /api/automations router (list/events/sessions/patch)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Startup wiring — ensure rows on lifespan start

**Files:**
- Modify: `server/app.py` (`_lifespan`: call `registry.ensure_rows()` after `init_db`)
- Test: `server/tests/test_automation_startup.py` (create)

**Interfaces:**
- Consumes: `registry.ensure_rows()` (Task 5).
- Produces: on app start, every registered automation has a row.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_automation_startup.py
"""App startup upserts automation rows (pr-doctor present after boot)."""
from __future__ import annotations

import os
from unittest.mock import patch
from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


def test_startup_creates_pr_doctor_row():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        with TestClient(app) as client:  # context-manager triggers lifespan startup
            r = client.get("/api/automations", headers=H)
            pd = next(a for a in r.json() if a["id"] == "pr-doctor")
            assert pd["enabled"] is False  # row exists with default state
```

- [ ] **Step 2: Run test to verify it fails (or is flaky)**

Run: `pytest server/tests/test_automation_startup.py -v`
Expected: the row is currently created lazily by `_row`, so to make this a real test of *startup* upsert, assert it via a direct DB read before any GET. Replace the assertion body with a `db_session` query for the `Automation` row right after entering the `TestClient` context, before issuing any request. It will FAIL until `ensure_rows()` runs in lifespan.

- [ ] **Step 3: Wire ensure_rows into lifespan**

In `server/app.py` `_lifespan` (after the DB is initialised, ~line 112-120), add:

```python
    from server.automations.registry import registry as _automation_registry
    try:
        await _automation_registry.ensure_rows()
    except Exception:
        _logging.getLogger("server").warning("startup: automation ensure_rows failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_automation_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole automation backend suite**

Run: `pytest server/tests/test_automation_models.py server/tests/test_automation_base.py server/tests/test_automation_context.py server/tests/test_automation_spawn.py server/tests/test_automation_registry.py server/tests/test_pr_doctor_automation.py server/tests/test_automations_api.py server/tests/test_automation_startup.py server/tests/test_orchestrator_origin.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/tests/test_automation_startup.py
git commit -m "feat(automation): upsert automation rows on startup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Migrate pr_autofix onto the registry (FINAL backend task)

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_pr_autofix_mode` reads the registry row; `_on_pr_change` loop-guard via `ctx` target-state + emits)
- Modify: `server/db/models.py` (remove `AppSettings.pr_autofix_mode`)
- Modify: `server/api/settings.py` (remove `prAutofixMode`)
- Test: `server/tests/test_pr_autofix_migration.py` (create)
- Verify: existing `server/tests/test_*autofix*` / pr-change tests still pass.

**Interfaces:**
- Consumes: `registry` row for `pr-doctor`; `AutomationContext` target-state + emit.
- Produces: pr_autofix mode + loop-guard fully sourced from the registry; `AppSettings.pr_autofix_mode` gone.

> **Loop-guard invariants to preserve (the whole point of this task):**
> 1. One auto-attempt per failure event.
> 2. Re-arm ONLY on `passing`/merged — NEVER on transient `pending`/`none`.
> 3. Mark-before-await: a draining / no-live-driver injection re-arms (does not consume the attempt).

- [ ] **Step 1: Write the failing migration tests**

```python
# server/tests/test_pr_autofix_migration.py
"""pr_autofix mode + loop-guard sourced from the registry; AppSettings field gone."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from sqlalchemy import select


@pytest.fixture
async def orch_env():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        from server.db import init_db
        from server.sessions.orchestrator import SessionOrchestrator
        from server.automations.registry import registry
        from server.tests.fakes.fake_factory import fake_sdk_factory
        await init_db("sqlite+aiosqlite:///:memory:")
        await registry.ensure_rows()
        yield SessionOrchestrator(sdk_factory=fake_sdk_factory())


async def test_mode_read_from_registry_row(orch_env):
    from server.db import db_session
    from server.db.models import Automation
    orch = orch_env
    assert await orch._pr_autofix_mode() == "propose"  # default
    async with db_session() as db:
        row = (await db.execute(select(Automation).where(Automation.id == "pr-doctor"))).scalar_one()
        row.mode = "auto"
    assert await orch._pr_autofix_mode() == "auto"


def test_appsettings_no_longer_has_pr_autofix_mode():
    from server.db.models import AppSettings
    assert not hasattr(AppSettings, "pr_autofix_mode")
```

Also add (or migrate from the existing autofix test file) explicit loop-guard cases driving `_on_pr_change` through the new durable store:
- failing → (auto-attempt) → failing again ⇒ exactly ONE injection;
- failing → pending → failing ⇒ still no second injection (no re-arm on pending);
- failing → passing → failing ⇒ a NEW injection (re-armed);
- failing with a draining/no-live-driver ⇒ falls back to notify AND re-arms (next failing can retry).
> Mirror the assertion style of the existing pr-change tests; inject a fake `request_pr_fix`/`send_prompt` to count injections.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_pr_autofix_migration.py -v`
Expected: FAIL — `_pr_autofix_mode` still reads `AppSettings`; `AppSettings` still has the attr.

- [ ] **Step 3: Re-point `_pr_autofix_mode` to the registry row**

Replace the body of `_pr_autofix_mode` (orchestrator.py ~1205) with:

```python
    async def _pr_autofix_mode(self) -> str:
        """Current pr-doctor mode from the automation registry row; NULL/unknown → 'propose'."""
        from sqlalchemy import select
        from server.db.models import Automation
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "pr-doctor"))).scalar_one_or_none()
            mode = (row.mode if row else None) or "propose"
        from server.sessions.pr_fix import VALID_AUTOFIX_MODES
        return mode if mode in VALID_AUTOFIX_MODES else "propose"
```

- [ ] **Step 4: Move the loop-guard onto durable per-target state + emit events**

In `_on_pr_change` (orchestrator.py ~728), construct a pr-doctor context once in `__init__`:
`self._pr_doctor_ctx = AutomationContext("pr-doctor", create_session=self.create_session)` (import `AutomationContext`). Then rewrite the guard to use `target = f"{repo}#{info.number}"` state instead of the in-memory `_pr_fix_attempts` set:
- on non-failing: if `passing`, clear that target's `attempted` flag (delete row or set `{}`); keep the existing `_pushed_pr` discard. Do NOT clear on `pending`/`none`.
- on failing in `auto` with a live driver: read target state; if not `attempted`, set `attempted=True` BEFORE awaiting, call `request_pr_fix`; on `"sent"` emit `kind="attempted"` and return; otherwise clear `attempted` (re-arm) and fall through.
- emit `kind="detected"` once per failure entry; emit `kind="recovered"` when transitioning to `passing` after a prior attempt.
> Keep `_pushed_pr` exactly as-is (notification debounce). Keep `request_pr_fix`/`build_pr_fix_prompt` UNCHANGED. Remove the now-unused `_pr_fix_attempts` set from `__init__` only after the new path is green.

- [ ] **Step 5: Delete `AppSettings.pr_autofix_mode` and the settings API field**

In `server/db/models.py` remove the `pr_autofix_mode` column (lines 188-190). In `server/api/settings.py` remove: the `prAutofixMode` field from `SettingsBody`, the `"prAutofixMode": ...` line in `_to_dict`, the `if body.prAutofixMode ...` block in PUT, and the now-unused `VALID_AUTOFIX_MODES as _VALID_AUTOFIX` import.

- [ ] **Step 6: Run the migration tests + existing autofix/pr-change tests**

Run: `pytest server/tests/test_pr_autofix_migration.py server/tests/ -k "autofix or pr_change or pr_fix or settings" -v`
Expected: PASS. Fix any existing test that referenced `pr_autofix_mode` to set the registry row instead.

- [ ] **Step 7: Verify the no-parallel-state gate + full suite**

```bash
grep -rn pr_autofix_mode server/ web/ ; echo "exit=$?"
```
Expected: NO matches (grep exit 1). Then run `pytest server/tests/` — all green.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/db/models.py server/api/settings.py server/tests/test_pr_autofix_migration.py
git commit -m "refactor(automation): migrate pr_autofix onto the registry; drop AppSettings.pr_autofix_mode

Loop-guard moves to durable automation_target_state; mode reads the pr-doctor
registry row; AppSettings.pr_autofix_mode + prAutofixMode API field removed
(no parallel state). Invariants preserved + tested: one attempt per failure,
no re-arm on pending, draining re-arms.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Frontend API client + hooks

**Files:**
- Create: `web/src/api/automations.ts`
- Modify: `web/src/api/hooks.ts` (add `QK.automations`, `useAutomations`, `useAutomationEvents`, `usePatchAutomation`; remove `prAutofixMode` from `AppSettings`)
- Test: `web/src/api/automations.test.ts` (create)

**Interfaces:**
- Produces: types `Automation`, `AutomationEvent`, `AutomationStatus`, `ConfigField`; `fetchAutomations()`, `fetchAutomationEvents(id, params)`, `patchAutomation(id, body)`; hooks `useAutomations()`, `useAutomationEvents(id)`, `usePatchAutomation()`.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/api/automations.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fetchAutomations, patchAutomation } from './automations'

afterEach(() => vi.restoreAllMocks())

describe('automations api', () => {
  it('fetchAutomations GETs /api/automations', async () => {
    const data = [{ id: 'pr-doctor', name: 'PR Doctor', modes: ['off', 'auto'] }]
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await fetchAutomations()
    expect(out[0].id).toBe('pr-doctor')
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/automations', expect.anything())
  })

  it('patchAutomation PATCHes the body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 'pr-doctor', enabled: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await patchAutomation('pr-doctor', { enabled: true })
    expect(out.enabled).toBe(true)
    const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.method).toBe('PATCH')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/api/automations.test.ts`
Expected: FAIL — cannot resolve `./automations`.

- [ ] **Step 3: Write the client**

Create `web/src/api/automations.ts`:

```typescript
import { api } from './client'

export interface AutomationStatus {
  text: string
  level: 'idle' | 'active' | 'warn' | 'error'
  counts: Record<string, number> | null
}

export interface ConfigField {
  key: string
  label: string
  type: 'bool' | 'int' | 'string' | 'enum' | 'repo-multiselect'
  default: unknown
  help: string
  choices: string[] | null
}

export interface Automation {
  id: string
  name: string
  description: string
  enabled: boolean
  mode: string | null
  modes: string[]
  status: AutomationStatus
  configSchema: ConfigField[]
  config: Record<string, unknown>
  lastActiveAt: string | null
}

export interface AutomationEvent {
  ts: string
  level: string
  kind: string | null
  summary: string
  target: string | null
  sessionId: string | null
}

export interface AutomationPatch {
  enabled?: boolean
  mode?: string
  config?: Record<string, unknown>
}

export function fetchAutomations(): Promise<Automation[]> {
  return api.get<Automation[]>('/automations')
}

export function fetchAutomationEvents(
  id: string, params: { target?: string; kind?: string; limit?: number } = {},
): Promise<AutomationEvent[]> {
  const q = new URLSearchParams()
  if (params.target) q.set('target', params.target)
  if (params.kind) q.set('kind', params.kind)
  if (params.limit) q.set('limit', String(params.limit))
  const qs = q.toString()
  return api.get<AutomationEvent[]>(`/automations/${id}/events${qs ? `?${qs}` : ''}`)
}

export function fetchSpawnedSessions(
  id: string,
): Promise<{ id: string; name: string; status: string; originRef: string | null }[]> {
  return api.get(`/automations/${id}/sessions`)
}

export function patchAutomation(id: string, body: AutomationPatch): Promise<Automation> {
  return api.patch<Automation>(`/automations/${id}`, body)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `vitest run src/api/automations.test.ts`
Expected: PASS.

- [ ] **Step 5: Add hooks + remove prAutofixMode from AppSettings**

In `web/src/api/hooks.ts`: add `automations: ['automations'] as const` and `automationEvents: (id: string) => ['automation-events', id] as const` to `QK`; remove `prAutofixMode: string` from the `AppSettings` interface (line ~126); add:

```typescript
import {
  fetchAutomations, fetchAutomationEvents, patchAutomation,
  type Automation, type AutomationEvent, type AutomationPatch,
} from './automations'

export function useAutomations() {
  return useQuery<Automation[]>({
    queryKey: QK.automations, queryFn: fetchAutomations, staleTime: 10_000 })
}

export function useAutomationEvents(id: string, enabled = true) {
  return useQuery<AutomationEvent[]>({
    queryKey: QK.automationEvents(id), queryFn: () => fetchAutomationEvents(id),
    enabled, staleTime: 10_000 })
}

export function usePatchAutomation() {
  const qc = useQueryClient()
  return useMutation<Automation, ApiRequestError, { id: string; body: AutomationPatch }>({
    mutationFn: ({ id, body }) => patchAutomation(id, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.automations }) },
  })
}
```

- [ ] **Step 6: Verify build + lint**

Run (in `web/`): `tsc --noEmit && eslint src/api/automations.ts src/api/hooks.ts && vitest run src/api/automations.test.ts`
Expected: clean. (tsc will flag any remaining `prAutofixMode` reference — fix in Task 12.)
> If `tsc` errors on a `SettingsPanel`/test reference to `prAutofixMode`, leave it for Task 12 by temporarily not removing the interface field here; otherwise remove it now and fix the reference in Task 12. Prefer removing now and completing Task 12 in the same session.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/automations.ts web/src/api/hooks.ts web/src/api/automations.test.ts
git commit -m "feat(automation): web api client + hooks for /api/automations

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: AutomationCard + AutomationView components

**Files:**
- Create: `web/src/components/AutomationCard.tsx`
- Create: `web/src/components/AutomationView.tsx`
- Test: `web/src/components/AutomationCard.test.tsx` (create)

**Interfaces:**
- Consumes: `useAutomations`, `useAutomationEvents`, `usePatchAutomation` (Task 10).
- Produces: `<AutomationView />` (default export of the tab body) rendering one `<AutomationCard automation={a} />` per automation. Card: collapsed (name + status line + toggle + mode select when `modes.length > 2`); expanded (events feed, config panel from `configSchema`).

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/components/AutomationCard.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AutomationCard } from './AutomationCard'
import * as hooks from '@/api/hooks'
import type { Automation } from '@/api/automations'

const PR_DOCTOR: Automation = {
  id: 'pr-doctor', name: 'PR Doctor', description: 'watches CI',
  enabled: false, mode: 'propose', modes: ['off', 'auto', 'propose'],
  status: { text: 'idle', level: 'idle', counts: null },
  configSchema: [], config: {}, lastActiveAt: null,
}

describe('AutomationCard', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders name, status, and a toggle', () => {
    vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
      { mutate: vi.fn() } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
    vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
      { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
    render(<AutomationCard automation={PR_DOCTOR} />)
    expect(screen.getByText('PR Doctor')).toBeInTheDocument()
    expect(screen.getByText('idle')).toBeInTheDocument()
    expect(screen.getByRole('switch')).toBeInTheDocument()
  })

  it('toggling enabled calls patch', () => {
    const mutate = vi.fn()
    vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
      { mutate } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
    vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
      { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
    render(<AutomationCard automation={PR_DOCTOR} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(mutate).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: true } })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/components/AutomationCard.test.tsx`
Expected: FAIL — cannot resolve `./AutomationCard`.

- [ ] **Step 3: Write the components**

Create `web/src/components/AutomationCard.tsx`:

```tsx
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useAutomationEvents, usePatchAutomation } from '@/api/hooks'
import type { Automation } from '@/api/automations'

const LEVEL_DOT: Record<string, string> = {
  idle: 'bg-gray-400', active: 'bg-blue-500', warn: 'bg-amber-500', error: 'bg-red-500',
}

export function AutomationCard({ automation: a }: { automation: Automation }) {
  const [open, setOpen] = useState(false)
  const patch = usePatchAutomation()
  const events = useAutomationEvents(a.id, open)

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center gap-3 p-4">
        <button
          aria-label={open ? 'Collapse' : 'Expand'}
          onClick={() => setOpen((v) => !v)}
          className="text-left flex-1"
        >
          <div className="font-medium">{a.name}</div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className={cn('inline-block w-2 h-2 rounded-full', LEVEL_DOT[a.status.level])} />
            {a.status.text}
          </div>
        </button>
        {a.modes.length > 2 && (
          <select
            aria-label="Mode"
            value={a.mode ?? ''}
            onChange={(e) => patch.mutate({ id: a.id, body: { mode: e.target.value } })}
            className="text-sm rounded border border-border bg-background px-2 py-1"
          >
            {a.modes.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        )}
        <button
          role="switch"
          aria-checked={a.enabled}
          onClick={() => patch.mutate({ id: a.id, body: { enabled: !a.enabled } })}
          className={cn('relative w-10 h-6 rounded-full transition-colors',
            a.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600')}
        >
          <span className={cn('absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform',
            a.enabled && 'translate-x-4')} />
        </button>
      </div>
      {open && (
        <div className="border-t border-border p-4 space-y-4">
          <section>
            <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Activity</h4>
            {(events.data ?? []).length === 0
              ? <p className="text-sm text-muted-foreground">No activity yet.</p>
              : <ul className="space-y-1 text-sm">
                  {(events.data ?? []).map((e, i) => (
                    <li key={i} className="flex gap-2">
                      <span className="text-muted-foreground tabular-nums">{e.ts.slice(11, 19)}</span>
                      {e.sessionId
                        ? <a href={`/session/${e.sessionId}`} className="text-blue-600 hover:underline">{e.summary}</a>
                        : <span>{e.summary}</span>}
                    </li>
                  ))}
                </ul>}
          </section>
          {a.configSchema.length > 0 && (
            <section>
              <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Configuration</h4>
              <p className="text-sm text-muted-foreground">
                {a.configSchema.map((f) => f.label).join(', ')}
              </p>
            </section>
          )}
        </div>
      )}
    </div>
  )
}
```

> The config panel above is a minimal read-only summary to keep this task focused; the editable schema-driven inputs (incl. `repo-multiselect`) are wired in Task 12 alongside the repos data source. If you prefer, fold the editable panel into this task — but keep the test green.

Create `web/src/components/AutomationView.tsx`:

```tsx
import { useEffect } from 'react'
import { useAutomations } from '@/api/hooks'
import { setSessionsViewing } from '@/api/sessionsViewing'
import { AutomationCard } from './AutomationCard'

export function AutomationView() {
  const { data, isLoading } = useAutomations()
  // No sessions-summary work needed here; leave the sessions-viewing flag off.
  useEffect(() => { setSessionsViewing(false) }, [])
  if (isLoading) return <p className="text-sm text-muted-foreground p-4">Loading…</p>
  return (
    <div className="space-y-3" role="tabpanel" id="tabpanel-automation" aria-labelledby="tab-automation">
      {(data ?? []).map((a) => <AutomationCard key={a.id} automation={a} />)}
    </div>
  )
}

export default AutomationView
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `vitest run src/components/AutomationCard.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Verify build + lint**

Run (in `web/`): `tsc --noEmit && eslint src/components/AutomationCard.tsx src/components/AutomationView.tsx`
Expected: clean. (Confirm `cn` import path matches the repo — `@/lib/utils` per `Dashboard.tsx`.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/AutomationCard.tsx web/src/components/AutomationView.tsx web/src/components/AutomationCard.test.tsx
git commit -m "feat(automation): AutomationCard + AutomationView (expandable cards)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: Tab + route wiring, settings cleanup, provenance badge

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (TabId, tabRefs, TAB_ORDER, tab button, render `AutomationView`)
- Modify: `web/src/App.tsx` (`/automation` route)
- Modify: `web/src/components/settings/SettingsPanel.tsx` (remove PR auto-fix dropdown)
- Modify: `web/src/components/SessionCard.tsx` (provenance badge)
- Test: `web/src/components/__tests__/automationTab.test.tsx` (create) — or extend an existing Dashboard test.

**Interfaces:**
- Consumes: `AutomationView` (Task 11); session `origin` field (must be exposed by the sessions API type — confirm `Session`/`SessionSummary` TS type includes `origin`/`originRef`; add if missing, sourced from the backend `Session` row).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/automationTab.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as hooks from '@/api/hooks'
import { Dashboard } from '../Dashboard'

// Minimal harness: render Dashboard, click the Automation tab, expect the panel.
describe('Automation tab', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(hooks, 'useAutomations').mockReturnValue(
      { data: [], isLoading: false } as unknown as ReturnType<typeof hooks.useAutomations>)
    // stub the other hooks Dashboard calls as needed (sessions, accounts, settings)…
  })

  it('shows the Automation tab and renders its panel on click', () => {
    render(<Dashboard activeTab="automation" />)
    expect(screen.getByRole('tab', { name: 'Automation' })).toBeInTheDocument()
    expect(screen.getByRole('tabpanel', { hidden: false })).toBeTruthy()
  })
})
```
> Note: match the existing Dashboard test harness (`web/src/components/__tests__/` or `App.test.tsx`) for how hooks are stubbed; reuse that setup rather than re-inventing it.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `vitest run src/components/__tests__/automationTab.test.tsx`
Expected: FAIL — no "Automation" tab / `'automation'` not assignable to `TabId`.

- [ ] **Step 3: Wire the tab into Dashboard**

In `web/src/components/Dashboard.tsx`:
1. `export type TabId = 'sessions' | 'accounts' | 'archived' | 'pipeline' | 'automation'`
2. Add a ref: `const automationTabRef = useRef<HTMLButtonElement>(null)` and add `automation: automationTabRef` to `tabRefs`.
3. `const TAB_ORDER: TabId[] = ['sessions', 'accounts', 'pipeline', 'automation', 'archived']`
4. Add the tab button after the Pipeline button (copy the Pipeline button markup, replacing `pipeline`→`automation`, label `Automation`, `ref={automationTabRef}`).
5. Import `AutomationView` (`import { AutomationView } from './AutomationView'`) and render it: `{activeTab === 'automation' && <AutomationView />}` next to the pipeline panel.

- [ ] **Step 4: Add the route**

In `web/src/App.tsx`, add inside `<Routes>`: `<Route path="/automation" element={<DashboardRoute tab="automation" />} />`. (`DashboardRoute` is generic — no other change.)

- [ ] **Step 5: Remove the PR auto-fix dropdown + finish the prAutofixMode removal**

In `web/src/components/settings/SettingsPanel.tsx`, delete the "PR auto-fix" dropdown block and any `prAutofixMode` read/write. Run `tsc --noEmit` and fix any remaining `prAutofixMode` references (this completes the removal started in Task 10). Verify: `grep -rn prAutofixMode web/src` → 0 hits.

- [ ] **Step 6: Add the session provenance badge**

In `web/src/components/SessionCard.tsx`, where session metadata badges render, add (guarded):

```tsx
{session.origin && (
  <a href="/automation"
     className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
     title={`Created by an automation (${session.originRef ?? ''})`}>
    Created by {session.origin === 'pr-doctor' ? 'PR Doctor' : session.origin}
  </a>
)}
```
Ensure the session TS type exposes `origin: string | null` and `originRef: string | null` (add to the type + the sessions API mapper if absent; backend already returns them on the `Session` row — confirm the sessions serializer includes them, and add if not).

- [ ] **Step 7: Run the tab test + full web suite**

Run (in `web/`): `tsc --noEmit && eslint src && vitest run`
Expected: ALL PASS, no type/lint errors, `grep -rn prAutofixMode web/src` → 0 hits.

- [ ] **Step 8: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/App.tsx web/src/components/settings/SettingsPanel.tsx web/src/components/SessionCard.tsx web/src/components/__tests__/automationTab.test.tsx
git commit -m "feat(automation): Automation tab + route, settings cleanup, session provenance badge

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final Verification (run before opening the PR)

- [ ] **Backend:** `pytest server/tests/` — all green.
- [ ] **Web (in `web/`):** `vitest run` && `tsc --noEmit` && `eslint src` — all green.
- [ ] **No parallel state (constitution VI):** `grep -rn pr_autofix_mode server/ web/` and `grep -rn prAutofixMode web/src` — both 0 hits.
- [ ] **Acceptance criteria** (spec §"Acceptance criteria") 1-7 each satisfied — tick them off explicitly.
- [ ] **Manual smoke:** start the app, open `/automation`, toggle pr-doctor mode, confirm `_on_pr_change` reads the new mode (and the Settings panel no longer shows the old dropdown).
- [ ] Complete the development branch per `superpowers:finishing-a-development-branch` (PR off `origin/master`, ready + auto-merge per CLAUDE.md).

---

## Spec Coverage Map

| Spec section | Task(s) |
|---|---|
| Contract (`Automation`, `AutomationStatus`, `ConfigSchema`) | 2 |
| `AutomationContext` emit/target-state | 3 |
| `spawn_session` de-dup + provenance | 4 |
| Registry + startup upsert | 5, 8 |
| pr-doctor reference automation | 6 |
| Data model (3 tables + 2 columns) | 1 |
| API (list/events/sessions/patch, filters, 404/422) | 7 |
| pr_autofix migration + loop-guard invariants + grep gate | 9 |
| Frontend client/hooks | 10 |
| Expandable cards (status, toggle, feed, config) | 11 |
| Tab/route, repo-multiselect config, settings cleanup, provenance badge | 11, 12 |
| Acceptance criteria 1-7 | 9 (gate), Final Verification |
| Out-of-scope (run-now, WS push, per-repo instances) | not implemented (by design) |
