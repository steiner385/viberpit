# Model-Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold a session pending human approval whenever a gated model (default: the fable tier) is selected — at launch or mid-session — with a configurable denylist and env→global→per-account policy.

**Architecture:** A single policy resolver (`resolve_model_policy`, mirroring `resolve_permission_mode`) is called at the two model-write chokepoints in the orchestrator (`create_session`, `set_model`). When it returns `require_approval`, the session is parked: launch-time as a new `pending_approval` status (capacity-exempt, no driver), mid-session by stashing `pending_model` on the still-running record. A REST endpoint resolves the hold (approve → spawn/apply; deny → close/clear). The frontend surfaces holds via a dashboard affordance and a sticky panel mirroring the permission-block pattern.

**Tech Stack:** Python FastAPI + SQLAlchemy(SQLite/Postgres) backend; React+Vite+TS frontend; pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-08-31-fable-approval-gate-design.md`

## Global Constraints

- **Governance:** constitution v1.1.0 — single source of truth / no parallel implementations (VI); verify against real code, not assumptions (III); TDD + quality gates (V).
- **Schema rule:** new columns MUST be nullable so `_reconcile_columns` backfills on both SQLite and Postgres without a hand migration. Adding only nullable columns (no new model) means the Postgres-compat lists (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`) do **not** change. Still add an Alembic revision as the reviewable record.
- **The gate lives in the orchestrator, not the HTTP route** — automations call the orchestrator directly and must be gated too.
- **Only one new status value:** `pending_approval`. Deny reuses the existing terminal status `"ended"` (member of `INACTIVE_SESSION_STATUSES`), never a new `denied` value.
- **Tier source of truth:** `model_tier()` in `server/sessions/model_catalog.py`; the web mirror is `web/src/session/modelName.ts` `TIERS`. Do not add a third tier definition.
- **Policy vocabulary:** action ∈ `{"allow", "require_approval", "block"}`; default action `require_approval`; default denylist `fable`.
- **`lint-and-test` is four steps:** `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web: `vitest run`, `tsc --noEmit`, `eslint` (run in `web/`).
- **Run server tests against the prebuilt venv** (`/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …`) with all `CCSWITCH_SESSIOND_*` unset; auth tests need `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.
- **Bash cwd hook:** the `prod-deploy-gate` hook is cwd-relative — wrap web commands as `(cd web && …)` and avoid `cd` into subdirectories for git/grep.

---

### Task 1: `model_tier()` — server-side tier extraction (single source of truth)

**Files:**
- Modify: `server/sessions/model_catalog.py` (append helper + import `re`)
- Test: `server/tests/test_model_catalog.py` (create if absent)

**Interfaces:**
- Produces: `model_tier(model: str | None) -> str | None` — returns `"opus"|"sonnet"|"haiku"|"fable"` for aliases (`fable`, `fable[1m]`, `opusplan`→`opus`), pinned ids (`claude-fable-5`→`fable`), else `None` (for `None`, `default`, `best`, unknown).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_model_catalog.py
import pytest
from server.sessions.model_catalog import model_tier

@pytest.mark.parametrize("model,expected", [
    (None, None),
    ("", None),
    ("fable", "fable"),
    ("Fable", "fable"),
    ("fable[1m]", "fable"),
    ("claude-fable-5", "fable"),
    ("opus", "opus"),
    ("opus[1m]", "opus"),
    ("opusplan", "opus"),
    ("claude-opus-4-8", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
    ("default", None),
    ("best", None),
    ("something-unknown", None),
])
def test_model_tier(model, expected):
    assert model_tier(model) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_model_catalog.py -v`
Expected: FAIL with `ImportError: cannot import name 'model_tier'`

- [ ] **Step 3: Write minimal implementation**

Add to `server/sessions/model_catalog.py` (ensure `import re` at top):

```python
# Canonical model tiers. The server mirror of web/src/session/modelName.ts TIERS
# (constitution VI: one tier definition, not three). Used by the model-approval
# policy to match a requested model against the gated-tier denylist regardless of
# whether it arrives as an alias ("fable", "fable[1m]") or a pinned id
# ("claude-fable-5").
_TIER_ALIASES: frozenset[str] = frozenset({"opus", "sonnet", "haiku", "fable"})


def model_tier(model: str | None) -> str | None:
    """The tier of a model alias or pinned id, or None when it has no fixed tier
    (None / "" / "default" / "best" / unknown ids)."""
    if not model:
        return None
    base = model.strip().lower().split("[", 1)[0]  # drop context tag like [1m]
    m = re.match(r"^claude-(opus|sonnet|haiku|fable)-", base)
    if m:
        return m.group(1)
    if base in _TIER_ALIASES:
        return base
    if base == "opusplan":
        return "opus"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_model_catalog.py -v` — Expected: PASS.
Then `ruff check server/sessions/model_catalog.py` and `mypy server/ --ignore-missing-imports`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/model_catalog.py server/tests/test_model_catalog.py
git commit -m "feat(models): add model_tier() tier-extraction helper (server SoT)"
```

---

### Task 2: Config env vars for the approval policy

**Files:**
- Modify: `server/config.py` (add two fields to `Settings`)
- Test: `server/tests/test_config_model_approval.py` (create)

**Interfaces:**
- Produces: `settings.model_approval_action: str` (default `"require_approval"`, alias `CCSWITCH_MODEL_APPROVAL_ACTION`) and `settings.model_approval_denylist: str` (default `"fable"`, alias `CCSWITCH_MODEL_APPROVAL_DENYLIST`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_config_model_approval.py
from server.config import Settings

def test_defaults():
    s = Settings()
    assert s.model_approval_action == "require_approval"
    assert s.model_approval_denylist == "fable"

def test_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_MODEL_APPROVAL_ACTION", "block")
    monkeypatch.setenv("CCSWITCH_MODEL_APPROVAL_DENYLIST", "fable,haiku")
    s = Settings()
    assert s.model_approval_action == "block"
    assert s.model_approval_denylist == "fable,haiku"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_config_model_approval.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'model_approval_action'`

- [ ] **Step 3: Write minimal implementation**

Add to the `Settings` class in `server/config.py` (near the other feature fields):

```python
    # Model-approval gate. Which model TIERS require human approval (comma list,
    # matched via model_catalog.model_tier), and the action a gated tier triggers.
    # Overridable per-install by AppSettings and per-account by the ledger; this is
    # the ops-level default (see server/sessions/model_policy.resolve_model_policy).
    model_approval_action: str = Field(
        default="require_approval", alias="CCSWITCH_MODEL_APPROVAL_ACTION")
    model_approval_denylist: str = Field(
        default="fable", alias="CCSWITCH_MODEL_APPROVAL_DENYLIST")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_config_model_approval.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_config_model_approval.py
git commit -m "feat(config): add model-approval action + denylist env vars"
```

---

### Task 3: Schema — Session + AppSettings columns, capacity exemption, Alembic revision

**Files:**
- Modify: `server/db/models.py` (Session columns, AppSettings columns, `CAPACITY_EXEMPT_STATUSES`)
- Create: `server/db/migrations/versions/0012_model_approval_columns.py`
- Test: `server/tests/test_model_approval_schema.py` (create)

**Interfaces:**
- Produces on `Session`: `pending_model: str | None`, `pending_model_requested_by: str | None`, `pending_model_requested_at: datetime | None`.
- Produces on `AppSettings`: `model_approval_action: str | None`, `model_approval_denylist: str | None`.
- Produces status value `"pending_approval"` added to `CAPACITY_EXEMPT_STATUSES`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_model_approval_schema.py
from server.db.models import Session, AppSettings, CAPACITY_EXEMPT_STATUSES

def test_session_has_pending_columns():
    cols = Session.__table__.columns
    for name in ("pending_model", "pending_model_requested_by", "pending_model_requested_at"):
        assert name in cols, name
        assert cols[name].nullable is True

def test_appsettings_has_policy_columns():
    cols = AppSettings.__table__.columns
    for name in ("model_approval_action", "model_approval_denylist"):
        assert name in cols, name
        assert cols[name].nullable is True

def test_pending_approval_is_capacity_exempt():
    assert "pending_approval" in CAPACITY_EXEMPT_STATUSES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_model_approval_schema.py -v` — Expected: FAIL (columns/status missing).

- [ ] **Step 3: Write minimal implementation**

In `server/db/models.py`, add to `class Session` (near the `model`/`archived_at` columns; keep `datetime`/`DateTime` imports as used elsewhere):

```python
    # Model-approval gate. When a gated model (see model_policy) is requested, the
    # requested model is parked here pending human approval — for a launch-time hold
    # the row's status is "pending_approval" (capacity-exempt, no driver); for a
    # running session it stays live on its current `model` while `pending_model`
    # holds the requested switch. All nullable so init_db backfills without a hand
    # migration on both dialects.
    pending_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pending_model_requested_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pending_model_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
```

Add to `class AppSettings`:

```python
    model_approval_action: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    model_approval_denylist: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

Extend the capacity-exempt set (a parked hold holds NO driver, so it is safe — invariant preserved):

```python
CAPACITY_EXEMPT_STATUSES: frozenset[str] = INACTIVE_SESSION_STATUSES | {"queued", "complete", "pending_approval"}
```

- [ ] **Step 4: Create the Alembic revision**

Create `server/db/migrations/versions/0012_model_approval_columns.py`:

```python
"""model-approval gate columns — sessions.pending_model* + app_settings policy

Revision ID: 0012_model_approval_columns
Revises: 0011_daemon_leases
Create Date: 2026-08-31

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs these nullable columns on both SQLite and Postgres
at next boot. All columns are nullable, so no default-synthesis / drift issue.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_model_approval_columns"
down_revision = "0011_daemon_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pending_model", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("pending_model_requested_by", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("pending_model_requested_at", sa.DateTime(), nullable=True))
    op.add_column("app_settings", sa.Column("model_approval_action", sa.String(), nullable=True))
    op.add_column("app_settings", sa.Column("model_approval_denylist", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "model_approval_denylist")
    op.drop_column("app_settings", "model_approval_action")
    op.drop_column("sessions", "pending_model_requested_at")
    op.drop_column("sessions", "pending_model_requested_by")
    op.drop_column("sessions", "pending_model")
```

- [ ] **Step 5: Run tests + schema-drift check**

Run: `pytest server/tests/test_model_approval_schema.py -v` — Expected: PASS.
Run the reconcile/drift coverage to confirm no Postgres-list update is needed:
`pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v` — Expected: PASS (all additions nullable, no new model). Then `ruff`/`mypy`.

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0012_model_approval_columns.py server/tests/test_model_approval_schema.py
git commit -m "feat(db): model-approval columns + pending_approval capacity exemption"
```

---

### Task 4: Ledger — per-account `modelApprovalAction`

**Files:**
- Modify: `src/ccswitch/ledger.py` (`set_account_defaults`, `get_account_defaults`)
- Test: `tests/test_ledger_model_approval.py` (create — this is a CORE test, runs under `pytest tests/`)

**Interfaces:**
- Consumes: existing `locked_ledger()`, `_acct()`.
- Produces: `set_account_defaults(..., model_approval_action=None, clear_model_approval_action=False)`; `get_account_defaults(led, name)["modelApprovalAction"]`.

> **NOTE:** per the CLAUDE.md editable-install caveat, run core tests with `PYTHONPATH=<checkout>/src` and confirm `python -c "import ccswitch.ledger as m; print(m.__file__)"` points at THIS checkout, not the deploy worktree.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger_model_approval.py
from ccswitch import ledger

def _led():
    return {"accounts": {"acct": {}}}

def test_default_is_none():
    assert ledger.get_account_defaults(_led(), "acct")["modelApprovalAction"] is None

def test_set_and_get():
    led = _led()
    ledger.set_account_defaults(led, "acct", model_approval_action="allow")
    assert ledger.get_account_defaults(led, "acct")["modelApprovalAction"] == "allow"

def test_clear():
    led = _led()
    ledger.set_account_defaults(led, "acct", model_approval_action="block")
    ledger.set_account_defaults(led, "acct", clear_model_approval_action=True)
    assert ledger.get_account_defaults(led, "acct")["modelApprovalAction"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/src pytest tests/test_ledger_model_approval.py -v`
Expected: FAIL with `TypeError: set_account_defaults() got an unexpected keyword argument 'model_approval_action'`

- [ ] **Step 3: Write minimal implementation**

In `src/ccswitch/ledger.py`, extend `set_account_defaults` signature and body:

```python
def set_account_defaults(
    led: dict,
    name: str,
    *,
    default_model: str | None = None,
    clear_model: bool = False,
    default_permission_mode: str | None = None,
    clear_permission_mode: bool = False,
    model_approval_action: str | None = None,
    clear_model_approval_action: bool = False,
    env_overrides: dict | None = None,
    clear_env_overrides: bool = False,
) -> None:
    """Set per-account spawn defaults in the ledger."""
    acct = _acct(led, name)
    if default_model is not None:
        acct["defaultModel"] = default_model
    elif clear_model:
        acct.pop("defaultModel", None)
    if default_permission_mode is not None:
        acct["defaultPermissionMode"] = default_permission_mode
    elif clear_permission_mode:
        acct.pop("defaultPermissionMode", None)
    if model_approval_action is not None:
        acct["modelApprovalAction"] = model_approval_action
    elif clear_model_approval_action:
        acct.pop("modelApprovalAction", None)
    if env_overrides is not None:
        acct["envOverrides"] = env_overrides
    elif clear_env_overrides:
        acct.pop("envOverrides", None)
```

And extend `get_account_defaults` return dict:

```python
    return {
        "defaultModel": acct.get("defaultModel"),
        "defaultPermissionMode": acct.get("defaultPermissionMode"),
        "modelApprovalAction": acct.get("modelApprovalAction"),
        "envOverrides": acct.get("envOverrides"),
    }
```

Mirror the new keyword in `src/ccswitch/accounts.py:set_account_defaults` if that module wraps the ledger one (grep `def set_account_defaults` in `accounts.py`; add `model_approval_action` / `clear_model_approval_action` passthrough to match the existing `default_model` passthrough).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$(pwd)/src pytest tests/test_ledger_model_approval.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/ledger.py src/ccswitch/accounts.py tests/test_ledger_model_approval.py
git commit -m "feat(ledger): per-account modelApprovalAction default"
```

---

### Task 5: `resolve_model_policy()` — the policy resolver

**Files:**
- Create: `server/sessions/model_policy.py`
- Test: `server/tests/test_model_policy.py` (create)

**Interfaces:**
- Consumes: `model_tier` (Task 1), `settings.model_approval_action/denylist` (Task 2), `AppSettings.model_approval_*` (Task 3), ledger `modelApprovalAction` (Task 4).
- Produces: `async resolve_model_policy(db, *, model: str | None, account: str | None = None) -> str` returning `"allow" | "require_approval" | "block"`. Also `VALID_ACTIONS: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_model_policy.py
import pytest
from server.sessions.model_policy import resolve_model_policy, VALID_ACTIONS
from server.db.models import AppSettings

pytestmark = pytest.mark.asyncio

async def test_non_gated_is_allow(db_session_factory):
    async with db_session_factory() as db:
        assert await resolve_model_policy(db, model="opus") == "allow"
        assert await resolve_model_policy(db, model=None) == "allow"

async def test_gated_default_requires_approval(db_session_factory):
    async with db_session_factory() as db:
        assert await resolve_model_policy(db, model="fable") == "require_approval"
        assert await resolve_model_policy(db, model="claude-fable-5") == "require_approval"
        assert await resolve_model_policy(db, model="fable[1m]") == "require_approval"

async def test_global_action_override(db_session_factory):
    async with db_session_factory() as db:
        db.add(AppSettings(id="singleton", model_approval_action="block"))
        await db.commit()
        assert await resolve_model_policy(db, model="fable") == "block"

async def test_global_denylist_override(db_session_factory):
    async with db_session_factory() as db:
        db.add(AppSettings(id="singleton", model_approval_denylist="haiku"))
        await db.commit()
        assert await resolve_model_policy(db, model="fable") == "allow"      # no longer gated
        assert await resolve_model_policy(db, model="haiku") == "require_approval"

async def test_valid_actions():
    assert VALID_ACTIONS == frozenset({"allow", "require_approval", "block"})
```

> If `db_session_factory` is not an existing fixture, reuse the fixture the other `server/tests/test_*` policy tests use to get an `AsyncSession` (grep `async def db_session` in `server/tests/conftest.py`) and match its name.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_model_policy.py -v` — Expected: FAIL with `ModuleNotFoundError: server.sessions.model_policy`.

- [ ] **Step 3: Write minimal implementation**

Create `server/sessions/model_policy.py`:

```python
"""Model-approval policy — the single source of truth for whether a requested
model requires human approval, mirroring resolve_permission_mode's precedence.

    denylist (which tiers are gated):   AppSettings → env
    action  (what a gated tier does):   per-account ledger → AppSettings → env

Called at every point that materialises a model onto a session
(SessionOrchestrator.create_session and .set_model), so no create path can drift.
"""
from __future__ import annotations

from sqlalchemy import select

from server.config import settings
from server.db.models import AppSettings
from server.sessions.model_catalog import model_tier

VALID_ACTIONS: frozenset[str] = frozenset({"allow", "require_approval", "block"})


def _parse_denylist(raw: str | None) -> set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


async def resolve_model_policy(db, *, model: str | None, account: str | None = None) -> str:
    """Return "allow" | "require_approval" | "block" for *model* on *account*."""
    tier = model_tier(model)
    if tier is None:
        return "allow"

    row = (await db.execute(
        select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()

    denylist = _parse_denylist(
        (row.model_approval_denylist if row is not None else None)
        or settings.model_approval_denylist)
    if tier not in denylist:
        return "allow"

    if account:
        from ccswitch import ledger as _ledger
        with _ledger.locked_ledger() as led:
            acct_action = _ledger.get_account_defaults(led, account).get("modelApprovalAction")
        if acct_action in VALID_ACTIONS:
            return acct_action

    row_action = row.model_approval_action if row is not None else None
    if row_action in VALID_ACTIONS:
        return row_action
    return settings.model_approval_action if settings.model_approval_action in VALID_ACTIONS else "require_approval"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_model_policy.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/model_policy.py server/tests/test_model_policy.py
git commit -m "feat(models): resolve_model_policy — env→global→account approval policy"
```

---

### Task 6: Orchestrator launch gate — hold on `create_session`

**Files:**
- Modify: `server/sessions/orchestrator.py` (`create_session`; add `_hold_for_approval`, `self._pending_approvals` init, a `ModelBlockedError`)
- Test: `server/tests/test_orchestrator_model_gate.py` (create)

**Interfaces:**
- Consumes: `resolve_model_policy` (Task 5); existing `_active_session_count`, `_max_active_sessions`, `_spawn_session`, `_enqueue_session`, `SessionRecord`, `db_session`, `DbSession`, `SYSTEM_IDENTITY`.
- Produces: `create_session` returns a `SessionRecord` with `status="pending_approval"`, `pending_model=<model>`, `model=None` when the policy gates; raises `ModelBlockedError` on `block`. `self._pending_approvals: dict[str, dict]` maps `session_id → spawn kwargs`. `SessionRecord` gains a `pending_model` field.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_model_gate.py — sketch; adapt fixtures to the
# existing orchestrator test harness (grep server/tests for how a bare
# SessionOrchestrator is built with a fake driver factory).
import pytest
pytestmark = pytest.mark.asyncio

async def test_gated_create_holds_pending(orch):
    rec = await orch.create_session(name="s", account=None, model="fable")
    assert rec.status == "pending_approval"
    assert rec.pending_model == "fable"
    assert rec.model is None
    # parked: consumes no capacity slot and started no driver
    assert orch._active_session_count() == 0
    assert rec.id in orch._pending_approvals

async def test_blocked_create_raises(orch, set_global_action):
    await set_global_action("block")
    from server.sessions.orchestrator import ModelBlockedError
    with pytest.raises(ModelBlockedError):
        await orch.create_session(name="s", account=None, model="fable")

async def test_allowed_model_spawns_normally(orch):
    rec = await orch.create_session(name="s", account=None, model="opus")
    assert rec.status != "pending_approval"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v` — Expected: FAIL (`pending_approval` never set / attribute missing).

- [ ] **Step 3: Write minimal implementation**

Add a `pending_model` field to the `SessionRecord` dataclass (find its definition; add `pending_model: str | None = None` alongside `model`). Ensure `_records` inits `self._pending_approvals: dict[str, dict] = {}` in `__init__`.

Add the exception near the other orchestrator exceptions:

```python
class ModelBlockedError(Exception):
    """A model whose approval policy is 'block' was requested."""
    def __init__(self, model: str | None):
        super().__init__(f"model {model!r} is blocked by policy")
        self.model = model
```

In `create_session`, immediately after the `session_id = str(uuid.uuid4())` line and BEFORE the capacity check, insert the gate:

```python
        # Model-approval gate. The single chokepoint for BOTH the API and
        # automations (which call this method directly). A gated model is parked
        # here rather than spawned; a blocked one is refused outright.
        from server.sessions.model_policy import resolve_model_policy
        async with db_session() as _db:
            _policy = await resolve_model_policy(_db, model=model, account=account)
        if _policy == "block":
            raise ModelBlockedError(model)
        if _policy == "require_approval":
            return await self._hold_for_approval(
                session_id, name=name, account=account, initial_prompt=initial_prompt,
                cols=cols, rows=rows, cwd=cwd, branch=branch, repo=repo,
                identity=identity, permission_mode=permission_mode,
                attachment_ids=attachment_ids, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref, model=model)
```

Add the helper (mirrors `_enqueue_session`'s persistence shape, but status `pending_approval`, no queue enqueue, model parked in `pending_model`):

```python
    async def _hold_for_approval(self, session_id: str, *, name, account,
                                 initial_prompt, cols, rows, cwd, branch, repo,
                                 identity, permission_mode, attachment_ids,
                                 name_is_auto, origin, origin_ref, model) -> SessionRecord:
        from datetime import datetime
        now = datetime.utcnow()
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=name, account_name=account or "",
                claude_session_id=None, status="pending_approval", pid=None,
                cwd=cwd or "", repo=repo, branch=None,
                permission_mode=permission_mode, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref,
                model=None, pending_model=model,
                pending_model_requested_by=identity, pending_model_requested_at=now,
                initial_prompt=initial_prompt))
            from server.audit import record_audit
            await record_audit(db, type="model_approval.requested", identity=identity,
                               detail={"session_id": session_id, "model": model,
                                       "account": account})
        # Keep full spawn kwargs for a clean approve path (attachments included).
        # Restart-durable minus attachments: rebuilt from the row if lost.
        self._pending_approvals[session_id] = dict(
            name=name, account=account, initial_prompt=initial_prompt, cols=cols,
            rows=rows, cwd=cwd, branch=branch, repo=repo, identity=identity,
            permission_mode=permission_mode, attachment_ids=attachment_ids,
            name_is_auto=name_is_auto, origin=origin, origin_ref=origin_ref, model=model)
        rec = SessionRecord(
            id=session_id, name=name, account=account or "", claude_session_id="",
            cols=cols, rows=rows, cwd=cwd or "", repo=repo, branch=None,
            status="pending_approval", name_is_auto=name_is_auto,
            updated_at=now, origin=origin, origin_ref=origin_ref,
            model=None, pending_model=model)
        self._records[session_id] = rec
        return rec
```

> Match the exact `SessionRecord(...)` and `DbSession(...)` kwargs to `_enqueue_session` (Explore quote item 5). If `SessionRecord` has required positional fields not shown, copy them from `_enqueue_session` verbatim.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_model_gate.py
git commit -m "feat(orchestrator): hold gated-model sessions pending approval at launch"
```

---

### Task 7: Orchestrator mid-session gate — hold on `set_model`

**Files:**
- Modify: `server/sessions/orchestrator.py` (`set_model`)
- Modify: `server/bridge/ws.py` (pass identity into `set_model`)
- Test: `server/tests/test_orchestrator_model_gate.py` (extend)

**Interfaces:**
- Consumes: `resolve_model_policy`; the existing `set_model(self, session_id, model, ...)` and its driver-apply path.
- Produces: `set_model` gains an `identity: str = SYSTEM_IDENTITY` keyword. On `require_approval` it sets `record.pending_model` + `pending_model_requested_by` and persists WITHOUT applying to the driver; on `block` raises `ModelBlockedError`; on `allow` behaves as today. Rotation is unaffected because `_reassign` calls the low-level `driver.set_model`, not this method.

- [ ] **Step 1: Write the failing test**

```python
async def test_gated_set_model_holds(orch, running_session):
    sid = running_session.id
    await orch.set_model(sid, "fable", identity="user@x")
    rec = orch._records[sid]
    assert rec.pending_model == "fable"
    assert rec.model != "fable"          # current model unchanged
    assert running_session.driver.set_model_calls == []   # driver NOT touched

async def test_allowed_set_model_applies(orch, running_session):
    sid = running_session.id
    await orch.set_model(sid, "opus", identity="user@x")
    assert running_session.driver.set_model_calls[-1] == "opus"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v -k set_model` — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

At the top of `set_model` (after resolving `record`/driver, before applying), add:

```python
        from server.sessions.model_policy import resolve_model_policy
        async with db_session() as _db:
            _policy = await resolve_model_policy(_db, model=model, account=record.account)
        if _policy == "block":
            raise ModelBlockedError(model)
        if _policy == "require_approval":
            from datetime import datetime
            record.pending_model = model
            record.pending_model_requested_by = getattr(record, "pending_model_requested_by", None)
            async with db_session() as db:
                row = await db.get(DbSession, session_id)
                if row is not None:
                    row.pending_model = model
                    row.pending_model_requested_by = identity
                    row.pending_model_requested_at = datetime.utcnow()
                from server.audit import record_audit
                await record_audit(db, type="model_approval.requested", identity=identity,
                                   detail={"session_id": session_id, "model": model,
                                           "account": record.account})
            await self._publish_session_meta(session_id)  # match existing meta-publish call
            return
```

Add `identity: str = SYSTEM_IDENTITY` to the `set_model` signature. In `server/bridge/ws.py` where `setModel` is handled, pass `identity=identity` (the WS handler already has the caller identity for audit — grep `set_model(` in `ws.py`). Use whatever the codebase's existing session-meta broadcast is instead of the sketch `_publish_session_meta` (grep how `set_model`'s `after_turn` path notifies clients today and reuse it).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/bridge/ws.py server/tests/test_orchestrator_model_gate.py
git commit -m "feat(orchestrator): hold gated mid-session model switch pending approval"
```

---

### Task 8: Orchestrator `resolve_model_approval()` — approve / deny

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `resolve_model_approval`)
- Test: `server/tests/test_orchestrator_model_gate.py` (extend)

**Interfaces:**
- Consumes: `_pending_approvals`, `_spawn_session`, `_enqueue_session`, `_active_session_count`, `_max_active_sessions`, driver `set_model`, `db_session`, `DbSession`.
- Produces: `async resolve_model_approval(self, session_id: str, decision: str, *, identity: str) -> SessionRecord`. `decision ∈ {"approve","deny"}`. Approve: launch-time → spawn/enqueue with the parked model; mid-session → apply `driver.set_model(pending_model)`. Deny: launch-time → status `"ended"`; mid-session → clear `pending_model`. Raises `KeyError` (unknown session) / `ValueError` (nothing pending / bad decision).

- [ ] **Step 1: Write the failing test**

```python
async def test_approve_launch_spawns(orch):
    rec = await orch.create_session(name="s", account=None, model="fable")
    out = await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
    assert out.status != "pending_approval"
    assert out.model == "fable"
    assert rec.id not in orch._pending_approvals

async def test_deny_launch_ends(orch):
    rec = await orch.create_session(name="s", account=None, model="fable")
    out = await orch.resolve_model_approval(rec.id, "deny", identity="user@x")
    assert out.status == "ended"
    assert out.pending_model is None

async def test_approve_midsession_applies(orch, running_session):
    sid = running_session.id
    await orch.set_model(sid, "fable", identity="user@x")
    await orch.resolve_model_approval(sid, "approve", identity="user@x")
    assert running_session.driver.set_model_calls[-1] == "fable"
    assert orch._records[sid].pending_model is None

async def test_deny_midsession_clears(orch, running_session):
    sid = running_session.id
    await orch.set_model(sid, "fable", identity="user@x")
    await orch.resolve_model_approval(sid, "deny", identity="user@x")
    assert orch._records[sid].pending_model is None
    assert running_session.driver.set_model_calls == []

async def test_resolve_without_pending_raises(orch, running_session):
    import pytest
    with pytest.raises(ValueError):
        await orch.resolve_model_approval(running_session.id, "approve", identity="user@x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v -k resolve` — Expected: FAIL (`AttributeError: resolve_model_approval`).

- [ ] **Step 3: Write minimal implementation**

```python
    async def resolve_model_approval(self, session_id: str, decision: str, *,
                                     identity: str) -> SessionRecord:
        if decision not in ("approve", "deny"):
            raise ValueError(f"bad decision: {decision!r}")
        rec = self._records.get(session_id)
        if rec is None:
            raise KeyError(session_id)

        from datetime import datetime
        from server.audit import record_audit

        # Launch-time hold — a not-yet-spawned pending_approval row.
        if rec.status == "pending_approval":
            kwargs = self._pending_approvals.pop(session_id, None) or \
                await self._rebuild_hold_kwargs(session_id)
            async with db_session() as db:
                await record_audit(db, type=f"model_approval.{decision}", identity=identity,
                                   detail={"session_id": session_id,
                                           "model": rec.pending_model})
            if decision == "deny":
                async with db_session() as db:
                    row = await db.get(DbSession, session_id)
                    if row is not None:
                        row.status = "ended"
                        row.pending_model = None
                rec.status = "ended"
                rec.pending_model = None
                return rec
            # approve: drop the placeholder, then run through the normal tail with
            # the parked model, reusing the same session_id.
            approved_model = rec.pending_model
            async with db_session() as db:
                row = await db.get(DbSession, session_id)
                if row is not None:
                    await db.delete(row)
            self._records.pop(session_id, None)
            kwargs.pop("model", None)
            if self._active_session_count() >= await self._max_active_sessions():
                return await self._enqueue_session(session_id, model=approved_model, **kwargs)
            return await self._spawn_session(session_id, model=approved_model, **kwargs)

        # Mid-session hold — a live session with a parked switch.
        if rec.pending_model:
            pending = rec.pending_model
            async with db_session() as db:
                await record_audit(db, type=f"model_approval.{decision}", identity=identity,
                                   detail={"session_id": session_id, "model": pending})
                row = await db.get(DbSession, session_id)
                if row is not None:
                    row.pending_model = None
                    if decision == "approve":
                        row.model = pending
            if decision == "approve":
                driver = self._drivers.get(session_id)  # match the field the code uses
                if driver is not None:
                    await driver.set_model(pending)
                rec.model = pending
            rec.pending_model = None
            await self._publish_session_meta(session_id)  # reuse existing broadcast
            return rec

        raise ValueError(f"session {session_id} has no pending model approval")

    async def _rebuild_hold_kwargs(self, session_id: str) -> dict:
        """Reconstruct spawn kwargs from the persisted row when the in-memory
        _pending_approvals entry was lost to a daemon restart (attachments are not
        recoverable and default to None)."""
        async with db_session() as db:
            row = await db.get(DbSession, session_id)
            if row is None:
                raise KeyError(session_id)
            return dict(
                name=row.name, account=row.account_name or None,
                initial_prompt=row.initial_prompt, cols=80, rows=24,
                cwd=row.cwd or None, branch=row.branch, repo=row.repo,
                identity=row.pending_model_requested_by or SYSTEM_IDENTITY,
                permission_mode=row.permission_mode, attachment_ids=None,
                name_is_auto=row.name_is_auto, origin=row.origin, origin_ref=row.origin_ref)
```

> Match `self._drivers` / driver lookup and `_publish_session_meta` to the real field/method names (grep `set_model` and its notify call).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_orchestrator_model_gate.py -v` — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_model_gate.py
git commit -m "feat(orchestrator): resolve_model_approval — approve spawns/applies, deny closes/clears"
```

---

### Task 9: API — expose `pendingModel` in the session serialization

**Files:**
- Modify: the session→dict serializer (grep `def _session_to_dict` / `def _serialize` in `server/api/sessions.py` and the fleet serializer if separate)
- Test: `server/tests/test_sessions_serialize_pending.py` (create)

**Interfaces:**
- Produces: session JSON gains `"pendingModel": <str|null>`. Status `"pending_approval"` passes through the existing `status` field unchanged.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sessions_serialize_pending.py
from server.api.sessions import _session_to_dict  # match the real serializer name

def test_pending_model_serialized(make_record):
    rec = make_record(status="pending_approval", pending_model="fable")
    d = _session_to_dict(rec)
    assert d["status"] == "pending_approval"
    assert d["pendingModel"] == "fable"
```

- [ ] **Step 2: Run test to verify it fails** — Expected: `KeyError: 'pendingModel'`.

- [ ] **Step 3: Write minimal implementation**

In the serializer, alongside the existing `"model": rec.model,` line add:

```python
        "pendingModel": getattr(rec, "pending_model", None),
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_serialize_pending.py
git commit -m "feat(api): surface pendingModel in session serialization"
```

---

### Task 10: API — `POST /api/sessions/{id}/model-approval`

**Files:**
- Modify: `server/api/sessions.py` (add route + `ModelApprovalBody`; map `ModelBlockedError`→409 on create)
- Test: `server/tests/test_api_model_approval.py` (create)

**Interfaces:**
- Consumes: `orch.resolve_model_approval` (Task 8), `current_identity`, `record_audit` (already emitted inside the orchestrator), `get_orchestrator`.
- Produces: `POST /api/sessions/{id}/model-approval` body `{"decision": "approve"|"deny"}` → 200 with the updated session dict; 404 unknown; 409 nothing pending. `ModelBlockedError` from `POST /api/sessions` maps to HTTP 409 with `{"detail": "model blocked by policy"}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_api_model_approval.py  (use the app's async test client + a
# gated model; mirror an existing server/tests/test_api_*.py for client + auth setup)
import pytest
pytestmark = pytest.mark.asyncio

async def test_approve_flow(client, gated_pending_session):
    r = await client.post(f"/api/sessions/{gated_pending_session}/model-approval",
                          json={"decision": "approve"})
    assert r.status_code == 200
    assert r.json()["status"] != "pending_approval"

async def test_bad_decision_422(client, gated_pending_session):
    r = await client.post(f"/api/sessions/{gated_pending_session}/model-approval",
                          json={"decision": "maybe"})
    assert r.status_code == 422

async def test_nothing_pending_409(client, running_session):
    r = await client.post(f"/api/sessions/{running_session}/model-approval",
                          json={"decision": "approve"})
    assert r.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails** — Expected: 404 (route missing).

- [ ] **Step 3: Write minimal implementation**

```python
from typing import Literal

class ModelApprovalBody(BaseModel):
    decision: Literal["approve", "deny"]


@router.post("/{session_id}/model-approval")
async def model_approval(
    session_id: str,
    body: ModelApprovalBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    orch = get_orchestrator()
    try:
        rec = await orch.resolve_model_approval(session_id, body.decision, identity=identity)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError:
        raise HTTPException(status_code=409, detail="no pending model approval")
    return _session_to_dict(rec)
```

And wrap the existing `orch.create_session(...)` call in `POST /api/sessions` (Explore quote item 2) to translate the block:

```python
    except ModelBlockedError:
        raise HTTPException(status_code=409, detail="model blocked by policy")
```

(import `ModelBlockedError` from `server.sessions.orchestrator`; add the `except` next to the existing `try` around `create_session`.)

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS. Then run the full auth-aware suite `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_api_model_approval.py -v`. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_api_model_approval.py
git commit -m "feat(api): POST /sessions/{id}/model-approval + block→409 on create"
```

---

### Task 11: Settings API — global action + denylist

**Files:**
- Modify: `server/api/settings.py` (`_to_dict`, `SettingsBody`, PUT handler)
- Test: `server/tests/test_settings_model_approval.py` (create)

**Interfaces:**
- Produces: settings JSON gains `"modelApprovalAction"` (str|null) + `"modelApprovalDenylist"` (str|null); PUT accepts and validates them (action ∈ `VALID_ACTIONS` or null).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_settings_model_approval.py
import pytest
pytestmark = pytest.mark.asyncio

async def test_roundtrip(client):
    r = await client.put("/api/settings", json={"modelApprovalAction": "block",
                                                "modelApprovalDenylist": "fable,haiku"})
    assert r.status_code == 200
    g = await client.get("/api/settings")
    assert g.json()["modelApprovalAction"] == "block"
    assert g.json()["modelApprovalDenylist"] == "fable,haiku"

async def test_invalid_action_rejected(client):
    r = await client.put("/api/settings", json={"modelApprovalAction": "nope"})
    assert r.status_code in (400, 422)
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (key missing / not persisted).

- [ ] **Step 3: Write minimal implementation**

In `_to_dict` add:

```python
        "modelApprovalAction": s.model_approval_action,
        "modelApprovalDenylist": s.model_approval_denylist,
```

Add to `SettingsBody`: `modelApprovalAction: str | None = None` and `modelApprovalDenylist: str | None = None`. In the PUT handler:

```python
    from server.sessions.model_policy import VALID_ACTIONS
    if "modelApprovalAction" in body.model_fields_set:
        if body.modelApprovalAction is not None and body.modelApprovalAction not in VALID_ACTIONS:
            raise HTTPException(status_code=422, detail="invalid modelApprovalAction")
        row.model_approval_action = body.modelApprovalAction
    if "modelApprovalDenylist" in body.model_fields_set:
        row.model_approval_denylist = body.modelApprovalDenylist
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS. Then `ruff`/`mypy`.

- [ ] **Step 5: Commit**

```bash
git add server/api/settings.py server/tests/test_settings_model_approval.py
git commit -m "feat(api): global model-approval action + denylist settings"
```

---

### Task 12: Accounts API — per-account `modelApprovalAction`

**Files:**
- Modify: `server/api/accounts.py` (`AccountPatchBody`, PATCH handler, `_account_response`/`AccountDefaults` surfacing)
- Test: `server/tests/test_accounts_model_approval.py` (create)

**Interfaces:**
- Produces: PATCH `/api/accounts/{name}` accepts `modelApprovalAction` (str|null, validated); account response `accountDefaults` includes it.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_accounts_model_approval.py
import pytest
pytestmark = pytest.mark.asyncio

async def test_set_account_action(client, an_account):
    r = await client.patch(f"/api/accounts/{an_account}", json={"modelApprovalAction": "allow"})
    assert r.status_code == 200
    assert r.json()["accountDefaults"]["modelApprovalAction"] == "allow"

async def test_invalid_rejected(client, an_account):
    r = await client.patch(f"/api/accounts/{an_account}", json={"modelApprovalAction": "nope"})
    assert r.status_code in (400, 422)
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Add to `AccountPatchBody`: `modelApprovalAction: str | None = None`. In the PATCH handler, extend the `if any(f in body.model_fields_set for f in (...))` set to include `"modelApprovalAction"`, validate it against `VALID_ACTIONS` (raise 422 if invalid and not None), and pass through:

```python
        model_approval_action=body.modelApprovalAction if "modelApprovalAction" in body.model_fields_set else None,
        clear_model_approval_action="modelApprovalAction" in body.model_fields_set and body.modelApprovalAction is None,
```

Ensure the account-defaults surfaced in `_account_response` (grep how `accountDefaults` is built) includes `modelApprovalAction` from `get_account_defaults`.

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS. Then `ruff`/`mypy`. Run the full `pytest server/tests/` once here to confirm no cross-file regression.

- [ ] **Step 5: Commit**

```bash
git add server/api/accounts.py server/tests/test_accounts_model_approval.py
git commit -m "feat(api): per-account modelApprovalAction override"
```

---

### Task 13: Frontend — types, api client, reducer, approval panel + dashboard affordance

**Files:**
- Modify: `web/src/api/types.ts` (`SessionStatus`, `Session`, `AccountDefaults`)
- Modify: `web/src/api/*` (add `postModelApproval`)
- Modify: `web/src/session/reducer.ts` (carry `pendingModel` from session meta)
- Create: `web/src/components/session/PendingModelApprovalPanel.tsx`
- Modify: dashboard session card component (grep the component rendering `queuePosition`/status badges)
- Test: `web/src/components/session/__tests__/PendingModelApprovalPanel.test.tsx` (create); extend an existing dashboard test.

**Interfaces:**
- Consumes: `Session.pendingModel`, `Session.status === 'pending_approval'`.
- Produces: `postModelApproval(sessionId: string, decision: 'approve' | 'deny'): Promise<Session>`; `<PendingModelApprovalPanel session onDecision />`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/PendingModelApprovalPanel.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { PendingModelApprovalPanel } from '../PendingModelApprovalPanel'

it('renders the pending model and fires decisions', () => {
  const onDecision = vi.fn()
  render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
  expect(screen.getByText(/Fable/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /approve/i }))
  expect(onDecision).toHaveBeenCalledWith('approve')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd web && vitest run src/components/session/__tests__/PendingModelApprovalPanel.test.tsx)` — Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

Add `'pending_approval'` to the `SessionStatus` union and `pendingModel?: string | null` to `Session` in `types.ts`; add `modelApprovalAction: string | null` to `AccountDefaults`.

Create `web/src/components/session/PendingModelApprovalPanel.tsx` (mirror `PendingPermissionPanel` styling — sticky, `role="region" aria-live="polite"`):

```tsx
import { modelName } from '../../session/modelName'

interface Props {
  model: string | null | undefined
  onDecision: (decision: 'approve' | 'deny') => void
}

export function PendingModelApprovalPanel({ model, onDecision }: Props) {
  const label = modelName(model) || model || 'this model'
  return (
    <div role="region" aria-live="polite" className="border-t p-3 flex items-center gap-3">
      <span className="text-sm">
        <strong>{label}</strong> requires approval before this session can use it.
      </span>
      <div className="ml-auto flex gap-2">
        <button onClick={() => onDecision('approve')}>Approve</button>
        <button onClick={() => onDecision('deny')}>Deny</button>
      </div>
    </div>
  )
}
```

Add the api client fn (match the existing fetch-wrapper style in `web/src/api`):

```ts
export async function postModelApproval(sessionId: string, decision: 'approve' | 'deny'): Promise<Session> {
  return apiFetch(`/api/sessions/${sessionId}/model-approval`, {
    method: 'POST', body: JSON.stringify({ decision }),
  })
}
```

In `reducer.ts` `sessionMeta` case, carry the pending field through:

```ts
    case 'sessionMeta': {
      return { ...state, model: ev.model, pendingModel: ev.pendingModel ?? null }
    }
```

(add `pendingModel?: string | null` to the `sessionMeta` event type). In the dashboard session card, when `session.status === 'pending_approval'` render an inline Approve/Deny affordance wired to `postModelApproval`, and render `<PendingModelApprovalPanel>` inside the session view when `session.pendingModel` is set on a running session.

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd web && vitest run src/components/session/__tests__/PendingModelApprovalPanel.test.tsx)` — Expected: PASS.
Run: `(cd web && tsc --noEmit && eslint src)` — Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): model-approval panel, types, api client, reducer wiring"
```

---

### Task 14: Frontend — New Session note + never-default-to-fable guard + settings/account controls

**Files:**
- Modify: New Session dialog component (grep the component behind the `NewSessionBranch.test.tsx` — likely `web/src/components/NewSession*.tsx`)
- Modify: settings UI (grep the component consuming `defaultPermissionMode` from `/api/settings`)
- Modify: account settings UI (grep the component consuming `AccountDefaults`)
- Test: extend `web/src/components/__tests__/NewSessionBranch.test.tsx`; add a settings + account test.

**Interfaces:**
- Consumes: models list `{id,label}`, `modelName`/`TIERS`, `AccountDefaults.modelApprovalAction`, settings `modelApprovalAction`/`modelApprovalDenylist`.

- [ ] **Step 1: Write the failing test**

```tsx
// in NewSessionBranch.test.tsx
it('never preselects a gated (fable) tier as the default model', () => {
  render(<NewSession /* ...existing test props with a models list incl fable */ />)
  const picker = screen.getByLabelText(/model/i) as HTMLSelectElement
  expect(picker.value).not.toBe('fable')
})

it('shows an approval note when fable is chosen', () => {
  render(<NewSession /* ... */ />)
  fireEvent.change(screen.getByLabelText(/model/i), { target: { value: 'fable' } })
  expect(screen.getByText(/requires approval/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd web && vitest run src/components/__tests__/NewSessionBranch.test.tsx)` — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

In the New Session dialog: ensure the initial selected model is never a gated tier — default to the account default or `'default'`, and if the computed initial value's `modelName` tier is gated, fall back to `'default'`. When the selected model's tier is gated (compute via the same `/^claude-|alias` logic — reuse `modelName` by checking the label, or add a tiny `isGatedTier(id)` helper next to `modelName.ts` `TIERS` that returns `TIERS`-keyed detection), render:

```tsx
{isGatedTier(selectedModel) && (
  <p className="text-xs text-amber-600">
    {modelName(selectedModel) || selectedModel} requires approval — this session will be held until approved.
  </p>
)}
```

Add `isGatedTier` to `web/src/session/modelName.ts` (web mirror of `model_tier`, matching the server denylist default of `fable`):

```ts
export function modelTier(id: string | null | undefined): keyof typeof TIERS | null {
  if (!id) return null
  const base = id.trim().toLowerCase().split('[')[0]
  const m = /^claude-(opus|sonnet|haiku|fable)-/.exec(base)
  if (m) return m[1] as keyof typeof TIERS
  if (base in TIERS) return base as keyof typeof TIERS
  if (base === 'opusplan') return 'opus'
  return null
}
// Client-side hint only; the server policy is authoritative. Default gated tier: fable.
export function isGatedTier(id: string | null | undefined): boolean {
  return modelTier(id) === 'fable'
}
```

In the settings UI add a select for `modelApprovalAction` (Require approval / Block / Allow) and a text field for `modelApprovalDenylist`, persisted via the existing settings PUT. In account settings add a per-account `modelApprovalAction` select (Inherit / Require approval / Block / Allow) persisted via the account PATCH.

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd web && vitest run)` for the touched tests — Expected: PASS.
Run the full web gate: `(cd web && vitest run && tsc --noEmit && eslint src)` — Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): new-session approval note, never-default-to-fable, policy settings UI"
```

---

### Final verification (before PR)

- [ ] **Backend gate (all four):** `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `PYTHONPATH=$(pwd)/src pytest tests/` · `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/` (run against the prebuilt venv, `CCSWITCH_SESSIOND_*` unset).
- [ ] **Web gate:** `(cd web && vitest run && tsc --noEmit && eslint src)`.
- [ ] **Manual smoke:** with defaults, create a session on `fable` → it lands `pending_approval`, consumes no slot; Approve → it spawns on fable; create another → Deny → it ends. Switch a running session to `fable` → held; Approve → applies. Set an account's `modelApprovalAction=allow` → fable on that account spawns directly.
- [ ] Open the PR (non-draft), then `gh pr merge <#> --squash --auto`. PR body: `Refs` the design spec; end with the Claude Code trailer.

## Self-review notes (coverage vs. spec)

- Policy resolver → Task 5; tier SoT → Task 1; config → Tasks 2/3/4/11/12 (env→global→account). ✓
- Launch gate + hold, capacity-exempt, no driver → Tasks 3/6. ✓
- Mid-session gate, rotation-exempt (uses `driver.set_model` below the gate) → Task 7. ✓
- Approve/deny (deny→`ended`, no new status) → Task 8. ✓
- REST + audit (`requested`/`approve`/`deny`) → Tasks 6/7/8 emit; Task 10 routes. ✓
- Frontend panel + dashboard + new-session note + never-default guard + settings/account UI → Tasks 13/14. ✓
- Schema durability + Alembic + Postgres-list no-op → Task 3. ✓
