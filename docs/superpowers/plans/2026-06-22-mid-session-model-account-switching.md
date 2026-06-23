# Mid-Session Model & Account Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user manually change both the model and the account of an individual live session, mid-session, from the web UI and via slash commands — reusing the existing rotation/recovery switch machinery.

**Architecture:** Manual account switch is a thin caller of the existing canonical primitive `orchestrator._reassign(record, new_account, *, reason)` (the same code path usage-ceiling rotation and 429/401 recovery already use). Model selection is persisted on the `Session` row and re-applied inside `_reassign`, so the chosen model survives every driver rebuild. A new `account_pinned` flag blocks usage-ceiling rotation but not hard-failure recovery. The web exposes two dropdowns that dispatch the same WebSocket `command` messages the `/model` and `/account` slash commands produce.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy (async, SQLite dev) · pytest · React + Vite + TypeScript · TanStack Query · Vitest.

## Global Constraints

- **Nullable columns only.** `create_all` never ALTERs existing tables; `init_db` backfills missing **nullable** columns. Both new columns (`model`, `account_pinned`) MUST be nullable — a NOT NULL column would need a real migration. (CLAUDE.md → Dev/testing.)
- **Route through `_reassign`.** Do NOT add a second interrupt→close→recompose→new-driver→resume→replay sequence. Manual switch calls the existing `_reassign`. (Constitution VI; CLAUDE.md → eliminating-parallel-implementations.)
- **Single source of truth for the model list.** The curated alias list is defined once in `server/sessions/model_catalog.py` and surfaced to the web via `GET /api/models`. No hardcoded model list in TypeScript.
- **Account = `CLAUDE_CONFIG_DIR`, per-process isolation.** Switching accounts means rebuilding the driver with a new `compose(account)` config dir — never mutating a live process's account. (Constitution II.)
- **TDD.** Write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- **Test env (server):** run from the repo root with the prebuilt interpreter. Plain unit tests: `python -m pytest <path> -v`. Tests that exercise API auth need `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest …`. A fresh worktree has no built env — use the main checkout's `.venv/bin/python` if `python -m pytest` cannot import `claude_agent_sdk`.
- **Test env (web):** run inside `web/`: `npx vitest run <file>`, `npx tsc --noEmit`, `npx eslint <file>`. Symlink `web/node_modules` from the main checkout if absent.

---

## File Structure

**Backend**
- `server/db/models.py` — add `model`, `account_pinned` nullable columns to `Session` (modify).
- `server/sessions/types.py` — add `model`, `account_pinned` to `SessionRecord` + dict round-trip (modify).
- `server/sessions/model_catalog.py` — curated alias list + validation helper (create).
- `server/api/models.py` — `GET /api/models` route (create).
- `server/sessions/orchestrator.py` — `set_model_for_session`, `switch_account_manual`; pin-aware `_rotation_monitor`; re-apply model + clear-pin-on-recovery in `_reassign`/`_rotate_away`; `/model` + `/account` routing in `run_command` (modify).
- `server/sessions/commands/catalog.py` — inject synthetic `/account` command (modify).
- `server/sessions/events.py` — extend `SessionMeta` wire payload with `account`, `account_pinned` (modify).

**Frontend**
- `web/src/api/types.ts` — `ModelOption` type (modify).
- `web/src/api/hooks.ts` — `useModels` hook (modify).
- `web/src/session/events.ts` — extend `EvSessionMeta` with `account`, `accountPinned` (modify).
- `web/src/session/reducer.ts` — store `account`/`accountPinned` from `sessionMeta` (modify).
- `web/src/components/session/SessionMeta.tsx` — model + account dropdowns, pin indicator (modify).
- `web/src/components/session/SessionView.tsx` — wire dropdown callbacks to `ws.command` (modify).

---

### Task 1: DB columns + SessionRecord fields

**Files:**
- Modify: `server/db/models.py:53-58` (Session model, add two columns)
- Modify: `server/sessions/types.py:24-56` (SessionRecord)
- Test: `server/tests/test_session_model_account_fields.py` (create)

**Interfaces:**
- Produces: `Session.model: str | None`, `Session.account_pinned: bool | None` (DB); `SessionRecord.model: str | None`, `SessionRecord.account_pinned: bool` (default `False`); both serialized in `SessionRecord.to_dict()` (`"model"`, `"accountPinned"`) and read in `from_dict`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_model_account_fields.py
from server.db.models import Session
from server.sessions.types import SessionRecord


def test_db_model_has_model_and_account_pinned_columns():
    cols = {c.name for c in Session.__table__.columns}
    assert "model" in cols
    assert "account_pinned" in cols
    # Must be nullable so init_db backfills without a migration.
    assert Session.__table__.c.model.nullable is True
    assert Session.__table__.c.account_pinned.nullable is True


def test_session_record_round_trips_model_and_pin():
    rec = SessionRecord(
        id="s1", name="n", account="acct-a", claude_session_id="c1",
        cols=80, rows=24, cwd="/tmp", model="opus", account_pinned=True,
    )
    d = rec.to_dict()
    assert d["model"] == "opus"
    assert d["accountPinned"] is True
    back = SessionRecord.from_dict(d)
    assert back.model == "opus"
    assert back.account_pinned is True


def test_session_record_defaults_are_none_and_false():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="c1",
        cols=80, rows=24, cwd="/tmp",
    )
    assert rec.model is None
    assert rec.account_pinned is False
    assert SessionRecord.from_dict(rec.to_dict()).account_pinned is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_session_model_account_fields.py -v`
Expected: FAIL — `AssertionError` ("model" not in cols) / `TypeError` (unexpected kwarg `model`).

- [ ] **Step 3: Add the DB columns**

In `server/db/models.py`, immediately after the `sub_agents` column (line 58), add:

```python
    # Persisted model for this session (alias like "opus"/"sonnet" or a pinned id
    # like "claude-haiku-4-5"). NULL = account/binary default. Nullable so init_db
    # backfills on the dev SQLite DB without a migration.
    model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # True once the user manually pinned the account: usage-ceiling rotation skips
    # the session (it goes 'limited' at the ceiling instead). Hard 401/429 recovery
    # still rotates and clears the pin. Nullable for the same backfill reason.
    account_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
```

- [ ] **Step 4: Add the SessionRecord fields**

In `server/sessions/types.py`, add to the defaults block (after line 31, `sub_agents`):

```python
    model: str | None = None         # persisted model alias/id; None = account default
    account_pinned: bool = False      # user pinned the account (skips usage-ceiling rotation)
```

In `to_dict` (after the `"adoptedInPlace"` entry), add:

```python
            "model": self.model,
            "accountPinned": self.account_pinned,
```

In `from_dict` (after `adopted_in_place=...`), add:

```python
            model=d.get("model"),
            account_pinned=d.get("accountPinned", False),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest server/tests/test_session_model_account_fields.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run the schema-reconcile invariant**

Run: `python -m pytest server/tests/test_schema_reconcile_coverage.py -v`
Expected: PASS (the new nullable columns are reconcilable).

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/tests/test_session_model_account_fields.py
git commit -m "feat(sessions): persist model + account_pinned on Session and SessionRecord"
```

---

### Task 2: Curated model catalog + `GET /api/models`

**Files:**
- Create: `server/sessions/model_catalog.py`
- Create: `server/api/models.py`
- Modify: `server/app.py` (register the new router — find where other `server.api.*` routers are `include_router`'d)
- Test: `server/tests/test_model_catalog.py` (create)
- Test: `server/tests/test_api_models.py` (create)

**Interfaces:**
- Produces: `model_catalog.ALIASES: list[str]` (`["default","opus","sonnet","haiku","opusplan"]`), `model_catalog.is_known_alias(name: str) -> bool`, `model_catalog.catalog() -> list[dict]` (each `{"id": str, "label": str}`). `GET /api/models` returns `list[{"id","label"}]`.

- [ ] **Step 1: Write the failing catalog test**

```python
# server/tests/test_model_catalog.py
from server.sessions import model_catalog as mc


def test_aliases_are_the_stable_binary_set():
    assert mc.ALIASES == ["default", "opus", "sonnet", "haiku", "opusplan"]


def test_catalog_returns_id_label_dicts():
    cat = mc.catalog()
    assert {"id": "opus", "label": "Opus"} in cat
    assert all(set(e) == {"id", "label"} for e in cat)


def test_is_known_alias():
    assert mc.is_known_alias("opus") is True
    assert mc.is_known_alias("claude-haiku-4-5") is False  # pinned ids are free-text, not aliases
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_model_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.model_catalog'`.

- [ ] **Step 3: Create the catalog module**

```python
# server/sessions/model_catalog.py
"""Curated model list for the session model selector — the single source of truth.

The claude-agent-sdk exposes no model catalog (set_model is an opaque pass-through
to the `claude` binary). The stable contract is the binary's /model aliases, which
auto-resolve to the latest and survive binary version bumps (pinned ids churn every
release). Pinned ids (e.g. "claude-haiku-4-5") remain accepted as free-text input;
they are simply not part of the curated alias list. Account authorization is enforced
by the binary/account, not here.
"""
from __future__ import annotations

# Stable aliases the `claude` /model picker accepts (verified against the binary).
ALIASES: list[str] = ["default", "opus", "sonnet", "haiku", "opusplan"]

_LABELS: dict[str, str] = {
    "default": "Default",
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "opusplan": "Opus Plan",
}


def catalog() -> list[dict]:
    """The curated list as [{"id", "label"}], in display order."""
    return [{"id": a, "label": _LABELS[a]} for a in ALIASES]


def is_known_alias(name: str) -> bool:
    return name in ALIASES
```

- [ ] **Step 4: Run catalog test to verify it passes**

Run: `python -m pytest server/tests/test_model_catalog.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Write the failing API test**

```python
# server/tests/test_api_models.py
from server.sessions import model_catalog as mc
from server.api.models import router


def test_router_is_mounted_at_api_models():
    assert router.prefix == "/api/models"


def test_route_returns_catalog():
    # The handler is a plain function returning the catalog; call it directly.
    import asyncio
    from server.api.models import get_models
    result = asyncio.run(get_models())
    assert result == mc.catalog()
```

- [ ] **Step 6: Run API test to verify it fails**

Run: `python -m pytest server/tests/test_api_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.api.models'`.

- [ ] **Step 7: Create the API route**

```python
# server/api/models.py
"""GET /api/models — the curated model list for the session model selector.

Single source of truth lives in server.sessions.model_catalog; this route just
exposes it so the web has no hardcoded TS model list.
"""
from __future__ import annotations

from fastapi import APIRouter

from server.sessions import model_catalog

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def get_models() -> list[dict]:
    """Return the curated model list as [{"id", "label"}]. No auth-sensitive data."""
    return model_catalog.catalog()
```

- [ ] **Step 8: Register the router in app.py**

In `server/app.py`, locate the block that imports and includes the other API routers (e.g. `from server.api import accounts` … `app.include_router(accounts.router)`). Add alongside them:

```python
from server.api import models as api_models
...
app.include_router(api_models.router)
```

- [ ] **Step 9: Run API test to verify it passes**

Run: `python -m pytest server/tests/test_api_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 10: Commit**

```bash
git add server/sessions/model_catalog.py server/api/models.py server/app.py \
        server/tests/test_model_catalog.py server/tests/test_api_models.py
git commit -m "feat(api): curated model catalog + GET /api/models (single source of truth)"
```

---

### Task 3: Persist the model when it is set

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `set_model_for_session`; route `/model` in `run_command`)
- Test: `server/tests/test_set_model_persists.py` (create)

**Interfaces:**
- Consumes: `SessionRecord.model` (Task 1); existing `self._drivers`, `self._records`, `db_session`, `DbSession` in the orchestrator.
- Produces: `orchestrator.set_model_for_session(session_id: str, model: str | None) -> None` — calls `driver.set_model(model)`, sets `record.model = model`, persists `Session.model`. `run_command` routes `name == "model"` to it.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_set_model_persists.py
import asyncio
import pytest
from server.sessions.types import SessionRecord


class _FakeDriver:
    def __init__(self): self.model = "untouched"
    async def set_model(self, m): self.model = m


@pytest.mark.asyncio
async def test_set_model_for_session_updates_record_and_driver(orchestrator_with_session):
    orch, sid, driver = orchestrator_with_session
    await orch.set_model_for_session(sid, "opus")
    assert driver.model == "opus"
    assert orch._records[sid].model == "opus"


@pytest.fixture
def orchestrator_with_session():
    # Build a minimal orchestrator instance without starting drivers/db.
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    driver = _FakeDriver()
    orch._records = {"s1": rec}
    orch._drivers = {"s1": driver}

    async def _noop_persist(sid, model):  # patched persistence
        rec.model = model
    orch._persist_model = _noop_persist  # type: ignore[attr-defined]
    return orch, "s1", driver
```

> Note: the test patches `_persist_model` to avoid a real DB. The implementation factors the DB write into `_persist_model(session_id, model)` so this seam exists.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_set_model_persists.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'set_model_for_session'`.

- [ ] **Step 3: Implement `set_model_for_session` + `_persist_model`**

In `server/sessions/orchestrator.py`, add these methods to `SessionOrchestrator` (place them just above `_reassign`, near the rotation/recovery block):

```python
    async def set_model_for_session(self, session_id: str, model: str | None) -> None:
        """Set the live model AND persist it, so it survives rehydrate and every
        driver rebuild (rotation/recovery re-applies record.model in _reassign)."""
        driver = self._drivers.get(session_id)
        if driver is not None:
            await driver.set_model(model)
        record = self._records.get(session_id)
        if record is not None:
            record.model = model
        await self._persist_model(session_id, model)

    async def _persist_model(self, session_id: str, model: str | None) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(model=model)
            )
```

- [ ] **Step 4: Route `/model` through the orchestrator in `run_command`**

In `server/sessions/orchestrator.py`, in `run_command` (line ~960), before the `return await cmd_dispatcher.dispatch(...)` line, add a special case so `/model` persists (the dispatcher's driver-only path does not):

```python
        if cmd.name == "model":
            arg = (args or "").strip()
            await self.set_model_for_session(session_id, arg or None)
            return {"name": "model", "render": "notice",
                    "data": {"text": f"Model → {arg or 'default'}"}}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest server/tests/test_set_model_persists.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Confirm the old driver-only path no longer double-handles model**

Run: `python -m pytest server/tests/test_command_dispatcher.py -v`
Expected: PASS — the dispatcher's `/model` branch is now dead for the orchestrator path but still valid in isolation; existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_set_model_persists.py
git commit -m "feat(sessions): persist model via orchestrator.set_model_for_session"
```

---

### Task 4: Re-apply the persisted model inside `_reassign`

**Files:**
- Modify: `server/sessions/orchestrator.py:1196-1201` (inside `_reassign`, after `new_driver.start()`)
- Test: `server/tests/test_reassign_reapplies_model.py` (create)

**Interfaces:**
- Consumes: `record.model` (Task 1); `_reassign` (existing).
- Produces: after a `_reassign`, the new driver has `set_model(record.model)` applied when `record.model` is set. No new public symbol.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_reassign_reapplies_model.py
import pytest


@pytest.mark.asyncio
async def test_reassign_reapplies_persisted_model(monkeypatch):
    # Capture the set_model calls on the rebuilt driver.
    from server.sessions import orchestrator as orch_mod

    calls = {}

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.claude_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): calls["model"] = m

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; claude_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    # Stub the collaborators _reassign reaches for.
    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model="sonnet")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {}
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_claude_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    # Stub DB write + event publish so we test only the model re-apply.
    async def _noop_db(self, *a, **k): pass
    orch._publish_event = lambda e: None
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    await orch._reassign(rec, "b", reason="manual")
    assert calls["model"] == "sonnet"


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, *a, **k): return None
```

> If the orchestrator's `_reassign` references collaborators by names other than those stubbed above (e.g. `ccswitch_accounts.sync_back`), add the corresponding `monkeypatch.setattr` for them — read `orchestrator.py:1185-1208` and stub each external call so only the model re-apply is exercised.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_reassign_reapplies_model.py -v`
Expected: FAIL — `KeyError: 'model'` (set_model was never called).

- [ ] **Step 3: Add the re-apply inside `_reassign`**

In `server/sessions/orchestrator.py`, inside `_reassign`, immediately after `await new_driver.start()` and `self._drivers[record.id] = new_driver` (line ~1197), before replaying the last prompt, add:

```python
            if record.model is not None:
                await new_driver.set_model(record.model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_reassign_reapplies_model.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the existing recovery/rotation tests (no regression)**

Run: `python -m pytest server/tests/test_orchestrator_recovery.py server/tests/test_driver_recovery.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_reassign_reapplies_model.py
git commit -m "feat(sessions): re-apply persisted model on every driver rebuild (_reassign)"
```

---

### Task 5: Manual account switch + pin-aware usage rotation + recovery clears the pin

**Files:**
- Modify: `server/sessions/orchestrator.py` (`switch_account_manual`; pin check in `_rotation_monitor`; clear pin in `_rotate_away`)
- Test: `server/tests/test_manual_switch_and_pin.py` (create)

**Interfaces:**
- Consumes: `SessionRecord.account_pinned` (Task 1); `_reassign`, `_rotate_away`, `_rotation_monitor` (existing); `_persist_status` (existing); existing module-level `_ROTATION_CEILING`.
- Produces: `orchestrator.switch_account_manual(session_id: str, new_account: str) -> None` — sets `record.account_pinned = True`, persists it, then `await self._reassign(record, new_account, reason="manual")`. `orchestrator._persist_pin(session_id, pinned)`. Usage-ceiling rotation skips a pinned session and sets it `limited`. `_rotate_away` (the failure path) clears `account_pinned`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_manual_switch_and_pin.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: object()}
    return orch


@pytest.mark.asyncio
async def test_switch_account_manual_pins_and_reassigns(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    seen = {}
    async def _reassign(self, r, new, *, reason):
        seen["new"] = new; seen["reason"] = reason; r.account = new
    async def _persist_pin(self, sid, pinned): seen["pinned"] = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    await orch.switch_account_manual("s1", "b")
    assert rec.account_pinned is True
    assert seen == {"new": "b", "reason": "manual", "pinned": True}


@pytest.mark.asyncio
async def test_rotate_away_clears_pin(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", account_pinned=True)
    orch = _orch_with(rec)
    orch._recovery = type("R", (), {
        "quarantined": lambda self, now: frozenset(),
        "record_rotation": lambda self, sid, now: None,
    })()
    orch._clock = lambda: 0.0
    async def _reassign(self, r, new, *, reason): r.account = new
    async def _persist_pin(self, sid, pinned): rec.account_pinned = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)
    monkeypatch.setattr("server.sessions.orchestrator.select_account", lambda exclude=frozenset(): "b")

    await orch._rotate_away(rec, reason="auth_401")
    assert rec.account_pinned is False  # hard-failure recovery overrides the pin
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_manual_switch_and_pin.py -v`
Expected: FAIL — `AttributeError: ... 'switch_account_manual'`.

- [ ] **Step 3: Add `switch_account_manual` + `_persist_pin`**

In `server/sessions/orchestrator.py`, add near `set_model_for_session`:

```python
    async def switch_account_manual(self, session_id: str, new_account: str) -> None:
        """User-initiated account switch. Pins the session against usage-ceiling
        rotation, then resumes the conversation on new_account via the shared
        _reassign primitive (same path as auto-rotation / recovery)."""
        record = self._records.get(session_id)
        if record is None or session_id not in self._drivers:
            raise KeyError(session_id)
        record.account_pinned = True
        await self._persist_pin(session_id, True)
        await self._reassign(record, new_account, reason="manual")

    async def _persist_pin(self, session_id: str, pinned: bool) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id)
                .values(account_pinned=pinned)
            )
```

- [ ] **Step 4: Make usage-ceiling rotation skip pinned sessions**

In `_rotation_monitor` (line ~1046), inside the `for record in ...` loop, after the `if record.id not in self._drivers: continue` guard, add the pin check that sets `limited` instead of rotating:

```python
                    if record.account_pinned:
                        log.info("rotation.skip_pinned session=%s account=%s", record.id, account)
                        record.status = "limited"
                        asyncio.create_task(
                            self._persist_status(record.id, "limited"),
                            name=f"limited-{record.id}",
                        )
                        self._publish_event({
                            "type": "status", "sessionId": record.id, "state": "limited",
                        })
                        continue
```

- [ ] **Step 5: Clear the pin when failure-recovery rotates a pinned session**

In `_rotate_away` (line ~1131), after a successful `select_account` and before/with `await self._reassign(...)`, clear the pin (recovery moved a pinned session off a broken account):

```python
        if record.account_pinned:
            record.account_pinned = False
            await self._persist_pin(record.id, False)
        self._recovery.record_rotation(record.id, now=now)
        await self._reassign(record, new_account, reason=reason)
```

(Replace the existing two lines `self._recovery.record_rotation(...)` / `await self._reassign(...)` with the block above.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_manual_switch_and_pin.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: No-regression on the recovery suite**

Run: `python -m pytest server/tests/test_orchestrator_recovery.py -v`
Expected: PASS (the `_rotate_away` path still rotates; pin-clear is additive).

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_manual_switch_and_pin.py
git commit -m "feat(sessions): manual account switch with pin; usage rotation respects pin, recovery overrides it"
```

---

### Task 6: `/account` slash command (dispatch + palette listing)

**Files:**
- Modify: `server/sessions/orchestrator.py` (`run_command`: route `name == "account"`)
- Modify: `server/sessions/commands/catalog.py` (inject synthetic `/account`)
- Test: `server/tests/test_account_command.py` (create)

**Interfaces:**
- Consumes: `switch_account_manual` (Task 5); `build_catalog` (existing).
- Produces: `run_command(session_id, "account", "<acct>")` → calls `switch_account_manual` and returns a notice payload. `build_catalog(...)` includes a synthetic `Command(name="account", kind="control")`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_account_command.py
import pytest
from server.sessions.commands.catalog import build_catalog
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def test_account_command_is_in_catalog():
    names = {c.name for c in build_catalog({"commands": []})}
    assert "account" in names
    acct = next(c for c in build_catalog({"commands": []}) if c.name == "account")
    assert acct.kind == "control"


@pytest.mark.asyncio
async def test_run_command_account_calls_manual_switch(monkeypatch):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": object()}
    seen = {}
    async def _switch(self, sid, acct): seen["acct"] = acct
    monkeypatch.setattr(SessionOrchestrator, "switch_account_manual", _switch)

    result = await orch.run_command("s1", "account", "acct-b")
    assert seen["acct"] == "acct-b"
    assert result["name"] == "account"
    assert result["render"] == "notice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_account_command.py -v`
Expected: FAIL — `account` not in catalog; `run_command` raises `KeyError("account")`.

- [ ] **Step 3: Inject the synthetic `/account` command into the catalog**

In `server/sessions/commands/catalog.py`, add a synthetic-command constant after `CONTROL` (line 13):

```python
# ccswitch-invented control commands not present in the SDK's get_server_info().
SYNTHETIC = [
    Command(name="account", description="Switch this session to another account",
            argument_hint="<account>", kind="control"),
]
```

(Ensure the `@dataclass class Command` definition appears before this `SYNTHETIC` constant — it already does in the current file.) Then, at the end of `build_catalog`, before `return out`, append any synthetic command not already present:

```python
    for syn in SYNTHETIC:
        if syn.name not in seen:
            seen.add(syn.name)
            out.append(syn)
```

- [ ] **Step 4: Route `/account` in `run_command`**

In `server/sessions/orchestrator.py`, in `run_command`, alongside the `/model` special-case from Task 3 (before the SDK-catalog lookup that would `KeyError`), add:

```python
        if str(name).lstrip("/") == "account":
            acct = (args or "").strip()
            if not acct:
                return {"name": "account", "render": "notice",
                        "data": {"text": "Usage: /account <account>"}}
            await self.switch_account_manual(session_id, acct)
            return {"name": "account", "render": "notice",
                    "data": {"text": f"Account → {acct}"}}
```

> Place this and the Task-3 `/model` case at the very top of `run_command` (after the `d = self._drivers.get(...)` / KeyError guard), so neither depends on the SDK catalog containing the command.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_account_command.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: No-regression on command tests**

Run: `python -m pytest server/tests/test_command_dispatcher.py server/tests/test_command_driver.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/sessions/commands/catalog.py server/tests/test_account_command.py
git commit -m "feat(sessions): /account slash command routes through manual switch"
```

---

### Task 7: Surface model + account + pin in the `sessionMeta` wire event

**Files:**
- Modify: `server/sessions/events.py:30-33` (`SessionMeta`)
- Modify: `server/sessions/translator.py:56-58` (emit account + pin with model)
- Test: `server/tests/test_session_meta_wire.py` (create)

**Interfaces:**
- Consumes: `SessionMeta` dataclass (existing).
- Produces: `SessionMeta(model, session_id, account=None, account_pinned=False)` whose `to_wire()` includes `"account"` and `"accountPinned"`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_meta_wire.py
from server.sessions.events import SessionMeta


def test_session_meta_wire_carries_account_and_pin():
    w = SessionMeta(model="opus", session_id="s1", account="acct-a", account_pinned=True).to_wire()
    assert w == {"type": "sessionMeta", "model": "opus", "sessionId": "s1",
                 "account": "acct-a", "accountPinned": True}


def test_session_meta_defaults_keep_account_optional():
    w = SessionMeta(model="opus", session_id="s1").to_wire()
    assert w["account"] is None
    assert w["accountPinned"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_session_meta_wire.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'account'`.

- [ ] **Step 3: Extend the `SessionMeta` dataclass**

Replace `server/sessions/events.py:30-33` with:

```python
@dataclass
class SessionMeta:
    model: str; session_id: str
    account: str | None = None
    account_pinned: bool = False
    def to_wire(self): return {"type": "sessionMeta", "model": self.model,
                               "sessionId": self.session_id, "account": self.account,
                               "accountPinned": self.account_pinned}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_session_meta_wire.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: No-regression on the translator**

Run: `python -m pytest server/tests/test_translator.py -v`
Expected: PASS (the translator still constructs `SessionMeta(model=..., session_id=...)`; new fields default).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/events.py server/tests/test_session_meta_wire.py
git commit -m "feat(sessions): sessionMeta wire event carries account + pin state"
```

---

### Task 8: Web — `ModelOption` type + `useModels` hook

**Files:**
- Modify: `web/src/api/types.ts` (add `ModelOption`)
- Modify: `web/src/api/hooks.ts:10-28` (add `QK.models` + `useModels`)
- Test: `web/src/api/hooks.models.test.tsx` (create)

**Interfaces:**
- Produces: `type ModelOption = { id: string; label: string }`; `useModels()` → TanStack query of `ModelOption[]` from `GET /api/models`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/api/hooks.models.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useModels } from './hooks'
import { api } from './client'

vi.mock('./client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useModels', () => {
  beforeEach(() => vi.clearAllMocks())
  it('fetches /models', async () => {
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue([{ id: 'opus', label: 'Opus' }])
    const { result } = renderHook(() => useModels(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/models')
    expect(result.current.data).toEqual([{ id: 'opus', label: 'Opus' }])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/api/hooks.models.test.tsx`
Expected: FAIL — `useModels` is not exported.

- [ ] **Step 3: Add the type**

In `web/src/api/types.ts`, add:

```ts
export interface ModelOption { id: string; label: string }
```

- [ ] **Step 4: Add the hook**

In `web/src/api/hooks.ts`, add `models: ['models'] as const,` to the `QK` object (after `accounts`), import the type, and add:

```ts
export function useModels() {
  return useQuery<ModelOption[]>({
    queryKey: QK.models,
    queryFn: () => api.get<ModelOption[]>('/models'),
    staleTime: 5 * 60_000,   // curated list rarely changes
  })
}
```

Update the type import at the top to include `ModelOption`.

- [ ] **Step 5: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/api/hooks.models.test.tsx`
Expected: PASS.

- [ ] **Step 6: Typecheck + lint**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/api/hooks.ts src/api/types.ts`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/hooks.models.test.tsx
git commit -m "feat(web): useModels hook + ModelOption type"
```

---

### Task 9: Web — carry account + pin through `sessionMeta` into session state

**Files:**
- Modify: `web/src/session/events.ts:16` (`EvSessionMeta`)
- Modify: `web/src/session/reducer.ts:59` (state shape) and `:478-480` (`sessionMeta` case)
- Test: `web/src/session/reducer.sessionMeta.test.ts` (create)

**Interfaces:**
- Consumes: `EvSessionMeta` (extended).
- Produces: `EvSessionMeta` adds `account?: string | null; accountPinned?: boolean`. Reducer state adds `accountPinned: boolean` and updates `account`/`accountPinned` from `sessionMeta`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/reducer.sessionMeta.test.ts
import { describe, it, expect } from 'vitest'
import { reducer, initialState } from './reducer'

describe('sessionMeta carries account + pin', () => {
  it('updates model, account, and accountPinned', () => {
    const s = reducer(initialState(), {
      type: 'sessionMeta', model: 'opus', sessionId: 's1',
      account: 'acct-b', accountPinned: true,
    } as never)
    expect(s.model).toBe('opus')
    expect(s.account).toBe('acct-b')
    expect(s.accountPinned).toBe(true)
  })
})
```

> If `initialState` is not an exported factory, adapt the import to however the reducer exposes its starting state (check the top of `reducer.ts`); the assertion logic is unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/session/reducer.sessionMeta.test.ts`
Expected: FAIL — `accountPinned` is undefined on state.

- [ ] **Step 3: Extend the event type**

In `web/src/session/events.ts:16`, replace `EvSessionMeta` with:

```ts
export interface EvSessionMeta   { type: 'sessionMeta';   model: string; sessionId: string; account?: string | null; accountPinned?: boolean }
```

- [ ] **Step 4: Extend reducer state + the `sessionMeta` case**

In `web/src/session/reducer.ts`, near line 59 (`model: string | null`), add to the state shape:

```ts
  account: string | null         // current account (from sessionMeta / session record)
  accountPinned: boolean         // user pinned the account (no usage-ceiling rotation)
```

Initialize both in the initial-state factory (`account: null`, `accountPinned: false`).
Replace the `sessionMeta` case (line ~478) with:

```ts
    case 'sessionMeta': {
      return {
        ...state,
        model: ev.model,
        account: ev.account ?? state.account,
        accountPinned: ev.accountPinned ?? state.accountPinned,
      }
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/session/reducer.sessionMeta.test.ts`
Expected: PASS.

- [ ] **Step 6: Typecheck + reducer suite**

Run (in `web/`): `npx tsc --noEmit && npx vitest run src/session/reducer`
Expected: clean / PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.sessionMeta.test.ts
git commit -m "feat(web): thread account + pin through sessionMeta into session state"
```

---

### Task 10: Web — model & account dropdowns in SessionMeta, wired to WS commands

**Files:**
- Modify: `web/src/components/session/SessionMeta.tsx` (replace the static `acct` / `model` spans with dropdowns + pin indicator)
- Modify: `web/src/components/session/SessionView.tsx` (pass `accounts`, `models`, `accountPinned`, and `onSelectModel`/`onSelectAccount` callbacks that call `ws.command(sessionId, name, args)`)
- Test: `web/src/components/session/SessionMeta.test.tsx` (create)

**Interfaces:**
- Consumes: `useModels` (Task 8), `useAccounts` (existing), `ws.command(sessionId, name, args)` (existing, `web/src/ws/client.ts:171`), reducer `account`/`accountPinned`/`model` (Task 9).
- Produces: `SessionMeta` gains props `models: ModelOption[]`, `accounts: { name: string }[]`, `accountPinned: boolean`, `onSelectModel: (id: string) => void`, `onSelectAccount: (name: string) => void`. Selecting an option calls the callback; SessionView maps those to `ws.command(sessionId, 'model'|'account', value)`.

- [ ] **Step 1: Write the failing component test**

```tsx
// web/src/components/session/SessionMeta.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionMeta } from './SessionMeta'

const base = {
  account: 'acct-a', model: 'opus', contextPct: null, quota: null, subAgents: [],
  models: [{ id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }],
  accounts: [{ name: 'acct-a' }, { name: 'acct-b' }],
  accountPinned: false,
}

describe('SessionMeta switchers', () => {
  it('dispatches a model selection', () => {
    const onSelectModel = vi.fn()
    render(<SessionMeta {...base} onSelectModel={onSelectModel} onSelectAccount={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Select model'), { target: { value: 'sonnet' } })
    expect(onSelectModel).toHaveBeenCalledWith('sonnet')
  })

  it('dispatches an account selection', () => {
    const onSelectAccount = vi.fn()
    render(<SessionMeta {...base} onSelectModel={vi.fn()} onSelectAccount={onSelectAccount} />)
    fireEvent.change(screen.getByLabelText('Switch account'), { target: { value: 'acct-b' } })
    expect(onSelectAccount).toHaveBeenCalledWith('acct-b')
  })

  it('shows a pin indicator when pinned', () => {
    render(<SessionMeta {...base} accountPinned onSelectModel={vi.fn()} onSelectAccount={vi.fn()} />)
    expect(screen.getByLabelText('Account pinned')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/SessionMeta.test.tsx`
Expected: FAIL — no `Select model` control / unknown props.

- [ ] **Step 3: Extend the SessionMeta props + render dropdowns**

In `web/src/components/session/SessionMeta.tsx`, extend `Props` (line 28) with:

```ts
  models: { id: string; label: string }[]
  accounts: { name: string }[]
  accountPinned: boolean
  onSelectModel: (id: string) => void
  onSelectAccount: (name: string) => void
```

Add them to the destructure on line 55. Replace the static **Account** span (lines 68-72) with an account `<select>` plus pin indicator:

```tsx
        {/* Account switcher */}
        <span className="inline-flex items-center gap-1">
          <span className="text-gray-400 dark:text-gray-500">acct</span>
          <select
            aria-label="Switch account"
            value={account}
            onChange={(e) => e.target.value && e.target.value !== account && onSelectAccount(e.target.value)}
            className="bg-transparent font-medium text-gray-900 dark:text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
          >
            {accounts.every((a) => a.name !== account) && <option value={account}>{account}</option>}
            {accounts.map((a) => (
              <option key={a.name} value={a.name}>{a.name}</option>
            ))}
          </select>
          {accountPinned && (
            <span aria-label="Account pinned" title="Pinned — auto-rotation off" className="text-amber-500">📌</span>
          )}
        </span>
```

Replace the static **Model** span (lines 74-78) with a model `<select>` that also allows a current pinned id not in the alias list:

```tsx
        {/* Model switcher */}
        <span className="inline-flex items-center gap-1">
          <span className="text-gray-400 dark:text-gray-500">model</span>
          <select
            aria-label="Select model"
            value={model ?? 'default'}
            onChange={(e) => onSelectModel(e.target.value)}
            className="bg-transparent font-medium text-gray-900 dark:text-gray-100 truncate max-w-[14rem] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
          >
            {model && !models.some((m) => m.id === model) && <option value={model}>{model}</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </span>
```

- [ ] **Step 4: Run component test to verify it passes**

Run (in `web/`): `npx vitest run src/components/session/SessionMeta.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire SessionView to supply data + dispatch commands**

In `web/src/components/session/SessionView.tsx`, where `<SessionMeta .../>` is rendered, call the hooks and pass the new props. Use the existing WS client (the same object that already exposes `command(sessionId, name, args)`):

```tsx
  const { data: models = [] } = useModels()
  const { data: accounts = [] } = useAccounts()
  // ...
  <SessionMeta
    account={state.account ?? account}
    model={state.model}
    accountPinned={state.accountPinned}
    models={models}
    accounts={accounts}
    onSelectModel={(id) => ws.command(sessionId, 'model', id)}
    onSelectAccount={(name) => ws.command(sessionId, 'account', name)}
    contextPct={/* existing */}
    quota={/* existing */}
    subAgents={/* existing */}
    statusSlot={/* existing */}
  />
```

> Use whatever names SessionView already has for `ws`, `sessionId`, and the existing SessionMeta props — keep those unchanged. Import `useModels` from `@/api/hooks`; `useAccounts` is likely already imported.

- [ ] **Step 6: Typecheck + lint + full session-component suite**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/components/session/SessionMeta.tsx src/components/session/SessionView.tsx && npx vitest run src/components/session`
Expected: clean / PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/SessionMeta.tsx web/src/components/session/SessionView.tsx web/src/components/session/SessionMeta.test.tsx
git commit -m "feat(web): model + account dropdowns in SessionMeta dispatch /model and /account"
```

---

### Task 11: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/ -q`
Expected: PASS (all green).

- [ ] **Step 2: Web suite**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: PASS / clean.

- [ ] **Step 3: Commit any incidental fixes, then open the PR per the CLAUDE.md completion workflow**

```bash
git push -u origin feat/mid-session-model-account-switch
gh pr create --title "Mid-session model & account switching" --body "$(cat <<'EOF'
Manual user-driven switching of both model and account for a live session.

- Manual account switch routes through the existing `_reassign` primitive (no parallel switch path).
- Model persisted on the Session row and re-applied inside `_reassign`, so it survives usage rotation and 429/401 recovery.
- `account_pinned` blocks usage-ceiling rotation; hard 401/429 recovery still rotates and clears the pin.
- Curated model list (aliases) is one source of truth in `model_catalog.py`, exposed via `GET /api/models`; free-text pinned ids still accepted.
- UI dropdowns + `/model` and `/account` slash commands share one WS `command` path.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

---

## Self-Review

**Spec coverage:**
- Single switch primitive (`_reassign`) — Tasks 4, 5 (route through it; no duplication). ✓
- DB `model` + `account_pinned` nullable columns + `SessionRecord` mirror — Task 1. ✓
- Pin semantics (usage rotation skips → `limited`; failure recovery rotates + clears pin) — Task 5. ✓
- Model persistence + re-apply on every rebuild — Tasks 3, 4. ✓
- Curated alias list, one source, `GET /api/models`, free-text escape hatch — Tasks 2, 8, 10. ✓
- Commands `/model` (persist) + `/account` — Tasks 3, 6. ✓
- Both surfaces sharing one WS `command` path — Task 10 (`ws.command`). ✓
- `rotated` event already exists; `sessionMeta` extended for account+pin — Task 7, 9. ✓
- Schema-reconcile coverage — Task 1 Step 6. ✓
- Full-suite verification — Task 11. ✓

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Steps that depend on local naming (SessionView prop names, `initialState` shape, extra `_reassign` collaborators to stub) carry an explicit inline note telling the implementer exactly what to check. ✓

**Type consistency:** `set_model_for_session`, `switch_account_manual`, `_persist_model`, `_persist_pin`, `_reassign(record, new_account, *, reason)`, `model_catalog.catalog()/ALIASES/is_known_alias`, `SessionMeta(model, session_id, account, account_pinned)`, `EvSessionMeta {model, sessionId, account?, accountPinned?}`, `useModels()`, `ModelOption {id,label}`, `ws.command(sessionId, name, args)` — names match across producing/consuming tasks. ✓
