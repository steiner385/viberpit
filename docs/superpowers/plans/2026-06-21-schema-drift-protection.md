# Schema-Drift Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Catch reconcile-incompatible schema changes before merge (CI) and before a bad deploy serves traffic (startup self-check + `/healthz` + auto-deploy rollback), and close the current `list`/`dict`/`datetime`-default reconcile gap.

**Architecture:** The live deploy runs SQLite + `_reconcile_sqlite_columns` (not Alembic). `_ddl_default_literal` is the single source of truth for "can reconcile synthesize this column?"; both the runtime reconcile and a new CI coverage test consult it. A baseline-upgrade test proves reconcile bridges an old populated DB to the current model. A startup self-check fails fast on residual drift, `/healthz` exposes it, and `auto-deploy.sh` rolls back if `/healthz` doesn't come up.

**Tech Stack:** Python 3.12 / SQLAlchemy 2 (async, aiosqlite) / FastAPI / pytest; bash (`auto-deploy.sh`).

## Global Constraints

- Reconcilable column ⇔ `column.nullable OR column.primary_key OR _ddl_default_literal(column) is not None OR "<table>.<col>" in RECONCILE_EXEMPT`.
- Callable-default synthesis (via `default.arg(None)`): `== []` → `'[]'`; `== {}` → `'{}'`; `datetime` instance → `CURRENT_TIMESTAMP`; else `None`.
- After Task 1, **every** current model column must be reconcilable with `RECONCILE_EXEMPT` empty.
- `/healthz` is **unauthenticated** (exempt from `IdentityMiddleware`), runs `select(Session).limit(1)`, returns `200 {"status":"ok"}` or `500 {"status":"schema_error","detail":...}`.
- Startup raises `SchemaDriftError` (subclass of `RuntimeError`) only on the SQLite path, after reconcile.
- Deploy host/port come from `$CCSWITCH_HOST` / `$CCSWITCH_PORT` (in `/etc/ccswitch/ccswitch.env`).
- Run backend tests: `.venv/bin/python -m pytest server/tests/ -q` (and `tests/` for the unittest suite). The two CI tests live under `server/tests/` so `ci.yml` and `auto-deploy.sh` run them automatically.

---

### Task 1: Reconcile synthesizes list / dict / datetime callable defaults

**Files:**
- Modify: `server/db/__init__.py` (`_ddl_default_literal`)
- Test: `server/tests/test_db_schema_reconcile.py` (add cases)

**Interfaces:**
- Produces: `_ddl_default_literal(column) -> str | None` now returns `'[]'`/`'{}'`/`CURRENT_TIMESTAMP` for the corresponding callable defaults (previously `None`).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_db_schema_reconcile.py`:

```python
def test_init_db_adds_not_null_json_column_with_list_default(tmp_path):
    """An old populated sessions table missing the NOT NULL `tool_allowlist`
    (JSON, default=list) column gains it, backfilled to '[]'."""
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT)"
    )
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd)"
                " VALUES ('o1','n','acct','idle','/tmp')")
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _read():
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT tool_allowlist FROM sessions WHERE id='o1'"))).scalar_one()
    # JSON column stored as TEXT; the backfilled default literal is '[]'.
    assert asyncio.run(_read()) == "[]"


def test_init_db_adds_not_null_datetime_column_with_current_timestamp(tmp_path):
    """An old sessions table missing the NOT NULL `created_at` (default=datetime.utcnow)
    gains it via CURRENT_TIMESTAMP rather than being skipped."""
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT)"
    )
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd)"
                " VALUES ('o2','n','acct','idle','/tmp')")
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _read():
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT created_at FROM sessions WHERE id='o2'"))).scalar_one()
    assert asyncio.run(_read()) is not None   # backfilled with a real timestamp
```

(The file already imports `asyncio`, `sqlite3`, `sa_text`, `init_db`, `db_session`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_db_schema_reconcile.py -q`
Expected: the two new tests FAIL — the columns are skipped (no `tool_allowlist`/`created_at` added → `OperationalError: no such column`).

- [ ] **Step 3: Implement**

In `server/db/__init__.py`, replace the body of `_ddl_default_literal` with:

```python
def _ddl_default_literal(column) -> str | None:
    """Return a SQL literal for `column`'s default, or None if it has none.

    SQLite can ADD a NOT NULL column to a populated table only when a DEFAULT is
    supplied to backfill existing rows. We synthesize that DEFAULT from the model
    default: scalar (bool/int/float/str) directly, and the common *callable*
    defaults by evaluating them — an empty list/dict becomes a JSON literal and a
    datetime-producing default becomes CURRENT_TIMESTAMP. A callable we can't map
    returns None so the caller skips it (and the CI coverage test flags it).
    """
    from datetime import datetime as _dt

    default = column.default
    if default is None:
        return None
    if getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return None
    if getattr(default, "is_callable", False):
        try:
            produced = default.arg(None)  # CallableColumnDefault takes an exec context
        except Exception:
            return None
        if isinstance(produced, _dt):
            return "CURRENT_TIMESTAMP"
        if produced == []:
            return "'[]'"
        if produced == {}:
            return "'{}'"
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_db_schema_reconcile.py -q`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add server/db/__init__.py server/tests/test_db_schema_reconcile.py
git commit -m "feat(db): reconcile NOT NULL list/dict/datetime callable defaults"
```

---

### Task 2: CI reconcile-coverage test

**Files:**
- Create: `server/tests/test_schema_reconcile_coverage.py`

**Interfaces:**
- Consumes: `server.db._ddl_default_literal`, `server.db.models.Base` (Task 1).
- Produces: `RECONCILE_EXEMPT: set[str]` (in the test module) — documented escape hatch, starts empty.

- [ ] **Step 1: Write the failing-then-passing test**

Create `server/tests/test_schema_reconcile_coverage.py`:

```python
"""Guard: every model column must survive the SQLite reconcile path.

The live deploy runs SQLite + _reconcile_sqlite_columns (no Alembic). Reconcile
can ALTER-ADD a NOT NULL column to an existing, populated table ONLY when it can
synthesize a DEFAULT (see _ddl_default_literal). A NOT NULL column it can't
synthesize is skipped at runtime → "no such column" against an old DB (the
2026-06-21 incident). This test fails the PR if such a column is introduced.
"""
from __future__ import annotations

from server.db import _ddl_default_literal
from server.db.models import Base

# Columns intentionally handled by a real migration rather than reconcile.
# Format: "<table>.<column>". Keep empty; add only with a migration + a comment.
RECONCILE_EXEMPT: set[str] = set()


def _is_reconcilable(column) -> bool:
    return (
        column.nullable
        or column.primary_key                       # part of CREATE TABLE, never ALTER-added
        or _ddl_default_literal(column) is not None
    )


def test_every_model_column_is_reconcilable():
    offenders = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            if key in RECONCILE_EXEMPT:
                continue
            if not _is_reconcilable(column):
                offenders.append(key)
    assert not offenders, (
        "NOT NULL column(s) the SQLite reconcile path cannot auto-add to an "
        f"existing DB: {offenders}. Give each a scalar/list/dict/datetime default "
        "reconcile can synthesize, or add it via a real migration and list it in "
        "RECONCILE_EXEMPT (with a comment)."
    )
```

- [ ] **Step 2: Run to verify it passes (post-Task-1)**

Run: `.venv/bin/python -m pytest server/tests/test_schema_reconcile_coverage.py -q`
Expected: PASS — after Task 1, `tool_allowlist`/`created_at`/`updated_at` are reconcilable.

- [ ] **Step 3: Verify the guard actually catches drift (temporary mutation)**

Sanity-check the test has teeth: temporarily add a bad column to the model, confirm RED, then revert.

```bash
# Add a NOT NULL column with an un-synthesizable default to server/db/models.py Session, e.g.:
#   bad: Mapped[str] = mapped_column(String, nullable=False, default=lambda: "x"[::-1])
.venv/bin/python -m pytest server/tests/test_schema_reconcile_coverage.py -q   # expect FAIL listing sessions.bad
git checkout server/db/models.py   # revert the temporary column
.venv/bin/python -m pytest server/tests/test_schema_reconcile_coverage.py -q   # expect PASS again
```

(Do NOT commit the temporary column. This step only proves the guard works.)

- [ ] **Step 4: Commit**

```bash
git add server/tests/test_schema_reconcile_coverage.py
git commit -m "test(db): CI guard — every model column must be reconcilable on SQLite"
```

---

### Task 3: CI baseline-upgrade test (end-to-end)

**Files:**
- Create: `server/tests/test_schema_upgrade_from_baseline.py`

**Interfaces:**
- Consumes: `server.db.init_db`, `server.db.db_session`, `server.db.models.Base`/`Session` (Tasks 1).

- [ ] **Step 1: Write the test**

Create `server/tests/test_schema_upgrade_from_baseline.py`:

```python
"""End-to-end: reconcile must bridge an OLD, populated sessions table up to the
current model — the exact scenario fresh-DB tests miss and the 2026-06-21
incident hit. The baseline is intentionally ancient (worst case); it never needs
updating as the model grows.
"""
from __future__ import annotations

import asyncio
import sqlite3

from sqlalchemy import inspect as sa_inspect, text as sa_text

from server.db import init_db, db_session
from server.db.models import Base, Session

# The oldest sessions shape we must still upgrade from (pre-repo/title/etc.).
_LEGACY_SESSIONS_DDL = (
    "CREATE TABLE sessions ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " account_name TEXT NOT NULL,"
    " status TEXT NOT NULL,"
    " cwd TEXT NOT NULL)"
)


def test_reconcile_upgrades_legacy_db_and_preserves_row(tmp_path):
    db_file = tmp_path / "legacy.db"
    con = sqlite3.connect(db_file)
    con.execute(_LEGACY_SESSIONS_DDL)
    con.execute(
        "INSERT INTO sessions (id, name, account_name, status, cwd)"
        " VALUES ('legacy1', 'My Session', 'acct1', 'idle', '/home/x')"
    )
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _check():
        # (a) every current Session model column now exists in the DB
        async with db_session() as db:
            conn = await db.connection()
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in sa_inspect(c).get_columns("sessions")})
        model_cols = {c.name for c in Session.__table__.columns}
        missing = model_cols - cols
        assert not missing, f"reconcile left columns missing: {missing}"

        # (b) the pre-existing row still reads back (round-trip against real data)
        async with db_session() as db:
            row = (await db.execute(
                sa_text("SELECT id, name, status FROM sessions WHERE id='legacy1'"))).first()
        assert row == ("legacy1", "My Session", "idle")

    asyncio.run(_check())
```

- [ ] **Step 2: Run to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_schema_upgrade_from_baseline.py -q`
Expected: PASS — reconcile (post-Task-1) adds every later column to the legacy table and the seed row survives.

- [ ] **Step 3: Commit**

```bash
git add server/tests/test_schema_upgrade_from_baseline.py
git commit -m "test(db): baseline-upgrade — reconcile bridges an old populated DB to current model"
```

---

### Task 4: Startup schema self-check (fail fast)

**Files:**
- Modify: `server/db/__init__.py` (add `SchemaDriftError`, `_assert_no_schema_drift`, wire into `init_db`)
- Test: `server/tests/test_db_schema_reconcile.py` (add a raise test)

**Interfaces:**
- Produces: `class SchemaDriftError(RuntimeError)`; `init_db` raises it if, after reconcile, any model column is still absent from its table (SQLite path only).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_db_schema_reconcile.py`:

```python
def test_init_db_raises_schema_drift_when_reconcile_cannot_fix(tmp_path, monkeypatch):
    """If reconcile fails to add a model column, init_db must fail LOUD (raise
    SchemaDriftError) rather than letting the server start and serve empty."""
    from server.db import SchemaDriftError
    db_file = tmp_path / "drift.db"
    con = sqlite3.connect(db_file)
    # Old table missing several NOT NULL model columns.
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, cwd TEXT)")
    con.commit(); con.close()

    # Disable reconcile so the drift is NOT healed → the self-check must catch it.
    monkeypatch.setattr("server.db._reconcile_sqlite_columns", lambda sync_conn: None)

    with pytest.raises(SchemaDriftError):
        asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))
```

(Add `import pytest` at the top of the file if not present.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_db_schema_reconcile.py::test_init_db_raises_schema_drift_when_reconcile_cannot_fix -q`
Expected: FAIL — `init_db` does not raise (no self-check yet; `SchemaDriftError` undefined → ImportError).

- [ ] **Step 3: Implement**

In `server/db/__init__.py`, add the error class near the top (after `log = ...`):

```python
class SchemaDriftError(RuntimeError):
    """Raised at startup when the live DB is missing model columns that reconcile
    could not add — a drifted server must refuse to start, not serve empty."""
```

Add the check function next to `_reconcile_sqlite_columns`:

```python
def _assert_no_schema_drift(sync_conn) -> None:
    """After reconcile, verify every model column exists in its table.

    A residual gap (a column reconcile could not synthesize) means queries
    selecting it will raise at runtime — so fail fast and loud here instead.
    """
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it — fully formed
        present = {col["name"] for col in inspector.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in present]
        if missing:
            drift[table.name] = missing
    if drift:
        raise SchemaDriftError(
            f"DB is missing model columns reconcile could not add: {drift}. "
            "Add a synthesizable default or a real migration."
        )
```

Wire it into `init_db` (SQLite path), after reconcile:

```python
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if url.startswith("sqlite"):
            await conn.run_sync(_reconcile_sqlite_columns)
            await conn.run_sync(_assert_no_schema_drift)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_db_schema_reconcile.py -q`
Expected: PASS (new raise test + all prior reconcile tests — the healthy ones don't drift, so no false raise).

- [ ] **Step 5: Commit**

```bash
git add server/db/__init__.py server/tests/test_db_schema_reconcile.py
git commit -m "feat(db): fail-fast SchemaDriftError on residual drift at startup"
```

---

### Task 5: Unauthenticated `/healthz` endpoint

**Files:**
- Modify: `server/app.py` (add `/healthz` route in `create_app`)
- Modify: `server/auth/middleware.py` (exempt `/healthz` from identity enforcement)
- Test: `server/tests/test_healthz.py`

**Interfaces:**
- Produces: `GET /healthz` → `200 {"status":"ok"}` (DB schema OK) or `500 {"status":"schema_error","detail":...}`; reachable with NO identity header.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_healthz.py`:

```python
import os
from unittest.mock import patch
from server.tests.fakes.fake_factory import fake_sdk_factory


def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    os.environ["CCSWITCH_HOME"] = str(tmp_path)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_healthz_ok_without_identity(tmp_path):
    """/healthz returns 200 with NO Tailscale identity header (it must be exempt
    from IdentityMiddleware so the deploy can probe it)."""
    client = _client(tmp_path)
    with client:
        r = client.get("/healthz")          # no identity header
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_500_on_schema_error(tmp_path, monkeypatch):
    """If the DB query fails (e.g. a missing column raises), /healthz reports
    500 schema_error rather than letting the exception escape."""
    client = _client(tmp_path)
    with client:
        def boom(*args, **kwargs):
            raise RuntimeError("no such column: sessions.tool_allowlist")
        # healthz resolves db_session dynamically via the module, so this is seen.
        monkeypatch.setattr("server.db.db_session", boom)
        r = client.get("/healthz")
    assert r.status_code == 500
    assert r.json()["status"] == "schema_error"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_healthz.py -q`
Expected: FAIL — `/healthz` returns 401 (middleware) / 404 (no route).

- [ ] **Step 3: Implement the middleware exemption**

In `server/auth/middleware.py`, at module level (near the top of the file) add:

```python
_EXEMPT_PATHS = frozenset({"/healthz"})
```

At the very start of `IdentityMiddleware.dispatch`, before the `verify_identity` call:

```python
    async def dispatch(self, request: Request, call_next):
        # Unauthenticated liveness/schema probe (used by auto-deploy health-check).
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        identity = verify_identity(request)
        ...  # unchanged
```

- [ ] **Step 4: Implement the route**

In `server/app.py`, add imports at the top with the other imports. Resolve
`db_session` via the **module** (not a direct name import) so the 500 test's
monkeypatch of `server.db.db_session` is honoured:

```python
from fastapi.responses import JSONResponse
from sqlalchemy import select as _sa_select
import server.db as _db
from server.db.models import Session as _SessionModel
```

In `create_app`, alongside the existing `@app.get("/api/health")`:

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        try:
            async with _db.db_session() as db:
                await db.execute(_sa_select(_SessionModel).limit(1))
            return JSONResponse({"status": "ok"})
        except Exception as exc:  # noqa: BLE001 — any DB/schema failure is unhealthy
            return JSONResponse(
                {"status": "schema_error", "detail": str(exc)}, status_code=500)
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_healthz.py -q`
Expected: PASS (200 unauthenticated; 500 after the table drop).

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/auth/middleware.py server/tests/test_healthz.py
git commit -m "feat(app): unauthenticated /healthz schema probe (exempt from identity mw)"
```

---

### Task 6: `auto-deploy.sh` health-check + rollback

**Files:**
- Modify: `scripts/auto-deploy.sh`

**Interfaces:**
- Consumes: `GET /healthz` (Task 5); `$CCSWITCH_HOST` / `$CCSWITCH_PORT` from the env file.

- [ ] **Step 1: Implement the health-check + rollback**

In `scripts/auto-deploy.sh`, replace the final restart block:

```bash
# All gates green — swap in the new code.
sudo systemctl restart ccswitch.service
echo "[deploy] live at ${REMOTE:0:9}"
```

with:

```bash
# All gates green — swap in the new code.
sudo systemctl restart ccswitch.service

# Post-restart health gate: the new process must answer /healthz (schema OK)
# within ~30s, else roll back to the previous commit. /healthz is unauthenticated
# and runs a schema-touching query, so a schema-drift boot fails this check.
HEALTH_URL="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
healthy=0
for _ in $(seq 1 15); do
  if curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "[deploy] /healthz never came up — ROLLING BACK ${REMOTE:0:9} -> ${LOCAL:0:9}" >&2
  git reset --hard "$LOCAL"
  ( cd web && npm run build )
  sudo systemctl restart ccswitch.service
  echo "[deploy] rolled back to ${LOCAL:0:9}" >&2
  exit 1
fi

echo "[deploy] live + healthy at ${REMOTE:0:9}"
```

- [ ] **Step 2: Lint the script**

Run: `bash -n scripts/auto-deploy.sh && echo "syntax OK"`
Run (if shellcheck is available): `shellcheck scripts/auto-deploy.sh || true`
Expected: `syntax OK`; no new shellcheck errors introduced.

- [ ] **Step 3: Verify the health-check contract holds**

The shell can't be unit-tested in-repo, but the `/healthz` contract it relies on is covered by `server/tests/test_healthz.py` (Task 5). Confirm:

Run: `.venv/bin/python -m pytest server/tests/test_healthz.py -q`
Expected: PASS — proves `/healthz` returns 200 when healthy (curl `-f` succeeds) and 500 when drifted (curl `-f` fails → triggers rollback).

- [ ] **Step 4: Commit**

```bash
git add scripts/auto-deploy.sh
git commit -m "feat(deploy): /healthz post-restart gate + auto-rollback to last-good SHA"
```

---

### Task 7: Full-suite verification

**Files:** none (verification)

- [ ] **Step 1: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ server/tests/ -q`
Expected: all green (the new schema-drift tests run as part of `server/tests/`, which is what `ci.yml` and `auto-deploy.sh` execute).

- [ ] **Step 2: Run lint/type gates that CI runs**

Run: `ruff check server/`
Run: `mypy server/ --ignore-missing-imports`
Expected: no new errors from the changed files.

- [ ] **Step 3: Confirm the CI workflow picks up the new tests (no ci.yml change needed)**

Read `.github/workflows/ci.yml` and confirm its `pytest server/tests/ -q` step will collect `test_schema_reconcile_coverage.py`, `test_schema_upgrade_from_baseline.py`, and `test_healthz.py` (they live under `server/tests/`). No edit required; note this in the report.

- [ ] **Step 4: Commit (if any lint fixups were needed)**

```bash
git add -A && git commit -m "chore: lint/type fixups for schema-drift protection" || echo "nothing to fix"
```

---

## Notes for the implementer

- `_ddl_default_literal` is the single source of truth — the coverage test (Task 2) and the runtime reconcile MUST both call it; never duplicate the logic.
- The startup self-check (Task 4) and `/healthz` (Task 5) are complementary: the self-check makes a drifted deploy crash-loop (so `/healthz` never 200s), and the rollback (Task 6) reverts it. A drifted DB therefore cannot silently serve an empty list.
- Do NOT add `alembic check` or touch the Postgres migration path — out of scope (deploy is SQLite).
- `RECONCILE_EXEMPT` must stay empty after Task 1; if a future column legitimately needs it, it requires a real migration + a comment.
