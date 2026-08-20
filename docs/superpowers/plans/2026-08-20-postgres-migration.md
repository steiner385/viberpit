# Postgres Migration Implementation Plan (sub-project 1b)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move ccswitch from a single-writer SQLite file to PostgreSQL on dobby, so the edge and
multiple session daemons can share one database — the hard prerequisite for every remaining
multi-host sub-project.

**Architecture:** The schema is already portable (verified: `create_all` builds all 26 tables on
PostgreSQL 16.14). One value-level defect blocks the move — seven naive `DateTime` columns are
defaulted with the timezone-*aware* `_utcnow()`, which asyncpg rejects. Fix those columns, add a
Postgres-backed test path, provision the database, then copy the data with a retention-scoped
one-shot script and cut over.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (async), asyncpg 0.31, PostgreSQL 16.14, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` (sub-project 1b).

## Global Constraints

- **TDD (Principle V).** Failing test first, watched fail, then implement.
- **The migration must be re-runnable.** Every step is idempotent; a partial run is resumable
  without duplicating rows.
- **The SQLite file is the rollback.** It is never deleted, moved, or modified by this work.
- **Nullable rule still applies.** `init_db` backfills only nullable columns; adding a NOT NULL
  column needs a real migration.
- **Test command:**
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_SESSIOND_SOCKET=/nonexistent/ccswitch-test.sock .venv/bin/python -m pytest <target> -q`
  The socket override is required whenever ccswitch is stopped — a stale
  `/run/ccswitch/sessiond.sock` otherwise breaks collection for 39 files (`server/app.py:348`).
- **`sudo -u postgres psql` is passwordless on dobby.** Verified.

## Verified findings (spike, 2026-08-20)

Run against real PostgreSQL 16.14 + asyncpg 0.31.0 on dobby:

| Check | Result |
|---|---|
| `Base.metadata.create_all` on Postgres | **OK — all 26 tables.** No JSON/Float/index-name/reserved-word problems |
| naive `DateTime` column ← aware `_utcnow()` value | **FAILS** — `asyncpg.exceptions.DataError: invalid input for query argument $1` |
| naive `DateTime` column compared against an aware value | **FAILS** — breaks *queries*, not just inserts |
| `DateTime(timezone=True)` column ← aware value | OK |
| `DateTime(timezone=True)` column ← **naive** value | **OK** — asyncpg assumes UTC |

The last two rows are why the fix is cheap: the failure is **asymmetric**, so widening a column can
only remove failures and never introduce them. No caller audit is required.

**The seven affected columns** (naive `DateTime`, defaulted `_utcnow`):

```
notification_channel.created_at        automation_event.ts
notification_rule.created_at           automation_target_state.updated_at
automation.updated_at                  ci_finding.first_seen_at
                                       ci_finding.last_seen_at
```

**Current data volume** (post-compaction): DB 8.5 GB; `ci_test_result` 11,137,024 rows;
`ci_test_daily` 56,393; `app_sessions` 41,934; `ci_job` 214,170; `ci_run` 68,068.

## Migration scope decision

Copy **only what the retention policy keeps**: all `failed`/`error` rows, `passed`/`skipped` rows
inside the 7-day raw window, the `ci_test_daily` rollup, and every other table in full. The dropped
rows are ones the running `_rollup_passed` job would fold into day-counts within a week anyway. The
SQLite file is retained as the archive of record.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pyproject.toml` | deps | Modify: add `asyncpg` to the `server` extra |
| `server/db/models.py` | ORM schema | Modify: widen the 7 columns to `DateTime(timezone=True)` |
| `server/tests/conftest.py` | test harness | Modify: add the `pg_engine` fixture |
| `server/tests/test_postgres_compat.py` | PG-backed coverage | Create |
| `.github/workflows/ci.yml` | CI | Modify: add a `postgres:16` service + `CCSWITCH_TEST_PG_URL` |
| `scripts/ccswitch-pg-provision.sh` | ops | Create: role + database + grants |
| `scripts/ccswitch-sqlite-to-pg.py` | migration | Create: retention-scoped, idempotent, resumable |
| `docs/runbooks/postgres-cutover.md` | ops | Create: cutover + rollback runbook |

---

### Task 1: Postgres-backed test harness

Nothing else can be TDD'd against Postgres until a test can reach one. Tests skip cleanly when
`CCSWITCH_TEST_PG_URL` is unset, so contributors without a local Postgres are unaffected.

**Files:**
- Modify: `server/tests/conftest.py`
- Create: `server/tests/test_postgres_compat.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: a session-scoped `pg_engine` fixture yielding an `AsyncEngine` bound to a scratch
  database, with all tables created and dropped around the session. Tasks 2 onward consume it.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_postgres_compat.py
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CCSWITCH_TEST_PG_URL"),
    reason="set CCSWITCH_TEST_PG_URL to run Postgres-backed tests",
)


@pytest.mark.asyncio
async def test_full_schema_builds_on_postgres(pg_engine):
    """Every mapped table must build on Postgres, not just SQLite."""
    from sqlalchemy import inspect as sa_inspect
    from server.db.models import Base

    async with pg_engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(sa_inspect(c).get_table_names()))

    expected = {t.name for t in Base.metadata.sorted_tables}
    missing = expected - names
    assert not missing, f"tables absent on Postgres: {sorted(missing)}"
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```bash
env CCSWITCH_TEST_PG_URL=postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  .venv/bin/python -m pytest server/tests/test_postgres_compat.py -q
```
Expected: FAIL — `fixture 'pg_engine' not found`.

- [ ] **Step 3: Add the fixture**

Append to `server/tests/conftest.py`:

```python
@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("CCSWITCH_TEST_PG_URL")
    if not url:
        pytest.skip("CCSWITCH_TEST_PG_URL not set")
    return url


@pytest_asyncio.fixture(scope="session")
async def pg_engine(pg_url):
    """A Postgres engine with the full schema, torn down after the session.

    Session-scoped because create_all/drop_all over 26 tables is far too slow to
    repeat per test. Tests that need isolation must clean up their own rows.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from server.db.models import Base

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
```

`os` and `pytest` are already imported in that file; **`pytest_asyncio` is not** — add it. There is
no `asyncio_mode` setting in `pyproject.toml`, so async tests use explicit `@pytest.mark.asyncio`
and async fixtures need the `@pytest_asyncio.fixture` decorator shown above, not `@pytest.fixture`.

- [ ] **Step 4: Create the local test database and run the test**

```bash
sudo -u postgres psql -q \
  -c "DROP DATABASE IF EXISTS ccswitch_test;" \
  -c "DROP ROLE IF EXISTS ccswitch_test;" \
  -c "CREATE ROLE ccswitch_test LOGIN PASSWORD 'test';" \
  -c "CREATE DATABASE ccswitch_test OWNER ccswitch_test;"

env CCSWITCH_TEST_PG_URL=postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  .venv/bin/python -m pytest server/tests/test_postgres_compat.py -q
```
Expected: PASS (1 test).

- [ ] **Step 5: Confirm it SKIPS without the env var**

Run: `.venv/bin/python -m pytest server/tests/test_postgres_compat.py -q`
Expected: `1 skipped` — never a failure. A contributor with no Postgres must not see a red suite.

- [ ] **Step 6: Add the CI service**

In `.github/workflows/ci.yml`, inside the `lint-and-test` job, add above `steps:`:

```yaml
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: ccswitch_test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: ccswitch_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
```

and set the env on the existing "Server tests" step:

```yaml
      - name: Server tests
        env:
          CCSWITCH_TEST_PG_URL: postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test
        run: python -m pytest server/tests/ -q --junitxml=test-results/server.xml
```

- [ ] **Step 7: Commit**

```bash
git add server/tests/conftest.py server/tests/test_postgres_compat.py .github/workflows/ci.yml
git commit -m "test(db): add Postgres-backed test harness"
```

---

### Task 2: Add asyncpg and widen the seven datetime columns

**Files:**
- Modify: `pyproject.toml`
- Modify: `server/db/models.py`
- Modify: `server/tests/test_postgres_compat.py`

**Interfaces:**
- Consumes: the `pg_engine` fixture from Task 1.
- Produces: no signature changes. Seven columns change type from `DateTime` to
  `DateTime(timezone=True)`; `_utcnow` is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_postgres_compat.py`:

```python
@pytest.mark.asyncio
async def test_utcnow_defaults_insert_and_query_on_postgres(pg_engine):
    """Every column defaulted with the AWARE _utcnow() must accept it.

    asyncpg raises DataError when an aware datetime is bound to a
    `timestamp without time zone` column — and it fails on comparisons too, not
    just inserts, so this exercises both.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import (AutomationEvent, AutomationTargetState, CiFinding,
                                  NotificationChannel, NotificationRule, _utcnow)

    Session = async_sessionmaker(pg_engine, expire_on_commit=False)
    # Required fields (NOT NULL, no default) are verified against the models:
    #   NotificationChannel.name/.url · AutomationEvent.summary · AutomationTargetState.target
    rows = [
        NotificationChannel(id="t-chan", name="t-chan", kind="webhook",
                            url="https://example.invalid/hook"),
        NotificationRule(id="t-rule"),
        AutomationEvent(automation_id="t-auto", summary="spike"),
        AutomationTargetState(automation_id="t-auto", target="t"),
        CiFinding(id="t-find", repo="o/r"),
    ]
    async with Session() as s:
        s.add_all(rows)
        await s.commit()                      # fails with DataError before the fix

    async with Session() as s:                # comparison path, also DataError-prone
        await s.execute(select(CiFinding).where(CiFinding.first_seen_at < _utcnow()))
        await s.execute(select(AutomationEvent).where(AutomationEvent.ts < _utcnow()))

    async with Session() as s:                # session-scoped fixture: clean up
        for r in rows:
            await s.delete(await s.merge(r))
        await s.commit()
```

> These constructors were checked against `server/db/models.py`: the only NOT-NULL-without-default
> fields on these five models are `NotificationChannel.name`/`.url`, `AutomationEvent.summary`, and
> `AutomationTargetState.target`, all supplied above. If one still rejects, read the model and add
> the minimal value — never relax a column's nullability to make a test pass.

- [ ] **Step 2: Run it to verify it fails**

Run:
```bash
env CCSWITCH_TEST_PG_URL=postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  .venv/bin/python -m pytest server/tests/test_postgres_compat.py -q
```
Expected: FAIL — `asyncpg.exceptions.DataError: invalid input for query argument $1`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, add to the `server` extra list, beside `aiosqlite`:

```toml
    "asyncpg",
```

- [ ] **Step 4: Widen the seven columns**

In `server/db/models.py`, change exactly these seven from `DateTime` to `DateTime(timezone=True)`,
leaving nullability, defaults, `onupdate`, and `index` untouched:

| Table | Column |
|---|---|
| `notification_channel` | `created_at` |
| `notification_rule` | `created_at` |
| `automation` | `updated_at` |
| `automation_event` | `ts` |
| `automation_target_state` | `updated_at` |
| `ci_finding` | `first_seen_at` |
| `ci_finding` | `last_seen_at` |

Add a comment above the first one:

```python
    # timezone=True because these default to _utcnow(), which is AWARE. asyncpg rejects
    # an aware datetime bound to `timestamp without time zone` — on SELECT comparisons as
    # well as INSERT. The reverse (naive value into a tz-aware column) is accepted and
    # treated as UTC, so widening is strictly safe: it removes failures and adds none.
```

Do NOT change columns defaulted with `datetime.utcnow` (naive) — those are already consistent.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
env CCSWITCH_TEST_PG_URL=postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  .venv/bin/python -m pytest server/tests/test_postgres_compat.py -q
```
Expected: PASS (2 tests).

- [ ] **Step 6: Prove SQLite is unaffected**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_SESSIOND_SOCKET=/nonexistent/ccswitch-test.sock \
  .venv/bin/python -m pytest server/tests/ -q
```
Expected: no NEW failures. Three failures are **pre-existing** and unrelated —
`test_copilot_proxy_resolve`, `test_eta_parity_live`, `test_library_api::test_skill_crud_roundtrip`
(they fail identically at merge base). Also run
`.venv/bin/python -m pytest server/tests/ -q -k "schema_reconcile or drift"` — must pass.

- [ ] **Step 7: Run mypy — CI runs it and the 1a plan forgot to**

Run: `.venv/bin/python -m mypy server/ --ignore-missing-imports`
Expected: `Success: no issues found`.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml server/db/models.py server/tests/test_postgres_compat.py
git commit -m "fix(db): widen _utcnow-defaulted columns to timezone-aware for Postgres"
```

---

### Task 3: Provision the production database

**Files:**
- Create: `scripts/ccswitch-pg-provision.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Provision the ccswitch Postgres role + database on dobby. Idempotent.
#   sudo -u postgres bash scripts/ccswitch-pg-provision.sh '<password>'
set -euo pipefail

PASS="${1:?usage: ccswitch-pg-provision.sh <password>}"
DB="${CCSWITCH_PG_DB:-ccswitch}"
ROLE="${CCSWITCH_PG_ROLE:-ccswitch}"

psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASS}';
  ELSE
    ALTER ROLE ${ROLE} PASSWORD '${PASS}';
  END IF;
END \$\$;
SQL

if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB} OWNER ${ROLE};"
fi

psql -v ON_ERROR_STOP=1 -d "${DB}" -c "GRANT ALL ON SCHEMA public TO ${ROLE};"
echo "provisioned: role=${ROLE} db=${DB}"
```

- [ ] **Step 2: Run it and verify connectivity**

```bash
PW=$(openssl rand -base64 24)
sudo -u postgres bash scripts/ccswitch-pg-provision.sh "$PW"
PGPASSWORD="$PW" psql -h 127.0.0.1 -U ccswitch -d ccswitch -tAc "SELECT current_user, current_database();"
```
Expected: `ccswitch|ccswitch`.

- [ ] **Step 3: Verify idempotency**

Re-run the provision command. Expected: succeeds again, no error.

- [ ] **Step 4: Record the URL in the service env (do NOT commit it)**

Append to `/etc/ccswitch/ccswitch.env` (root-only, 0600) — **commented out** for now; Task 5 activates it:

```
# CCSWITCH_DB_URL=postgresql+asyncpg://ccswitch:<password>@127.0.0.1:5432/ccswitch
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ccswitch-pg-provision.sh
git commit -m "chore(db): add Postgres provisioning script"
```

---

### Task 4: Retention-scoped migration script

**Files:**
- Create: `scripts/ccswitch-sqlite-to-pg.py`

**Interfaces:**
- Consumes: the widened schema from Task 2 and the database from Task 3.
- Produces: a CLI with `--dry-run` (default) and `--apply`, plus `--verify` to re-check row counts.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""One-shot SQLite -> Postgres copy for ccswitch. Idempotent and resumable.

Retention-scoped by design (spec sub-project 1b): ci_test_result carries ~11.1M rows,
almost all of which the running _rollup_passed job folds into ci_test_daily within a
week. Copying them would be slow and pointless, so we copy only what retention keeps:
every failed/error row, plus passed/skipped rows inside the raw window.

The SQLite file is never modified. It remains the rollback and the archive.

  python scripts/ccswitch-sqlite-to-pg.py                 # dry run: report counts
  python scripts/ccswitch-sqlite-to-pg.py --apply         # copy
  python scripts/ccswitch-sqlite-to-pg.py --verify        # compare counts only
"""
import argparse
import asyncio
import os
import sqlite3
import time

from sqlalchemy.ext.asyncio import create_async_engine

from server.db.models import Base

SQLITE = os.environ.get("CCSWITCH_SQLITE_PATH",
                        "/home/tony/.claude-accounts/ccswitch.db")
BATCH = 5_000


def _retention_where(table: str, raw_days: float) -> str:
    """Only ci_test_result is scoped; every other table copies in full."""
    if table != "ci_test_result":
        return ""
    horizon = time.time() - raw_days * 86400.0
    # Mirrors _rollup_passed: failures are kept raw forever, the rest only in-window.
    return (f" WHERE outcome IN ('failed','error') OR created_ts >= {horizon}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-url", default=os.environ.get("CCSWITCH_DB_URL"))
    ap.add_argument("--raw-days", type=float, default=7.0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if not args.pg_url:
        raise SystemExit("need --pg-url or CCSWITCH_DB_URL")

    src = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)  # READ-ONLY
    src.row_factory = sqlite3.Row
    engine = create_async_engine(args.pg_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    for table in Base.metadata.sorted_tables:
        if table.name not in have:
            print(f"  {table.name:<28} absent in SQLite — skipped")
            continue
        where = _retention_where(table.name, args.raw_days)
        n = src.execute(f"SELECT COUNT(*) FROM \"{table.name}\"{where}").fetchone()[0]

        if args.verify or not args.apply:
            async with engine.connect() as c:
                from sqlalchemy import func, select as sa_select
                dst = (await c.execute(sa_select(func.count()).select_from(table))).scalar_one()
            flag = "OK " if dst == n else "DIFF"
            print(f"  {flag} {table.name:<28} sqlite={n:<10} pg={dst}")
            continue

        cols = [c.name for c in table.columns]
        cur = src.execute(
            f"SELECT {', '.join(chr(34)+c+chr(34) for c in cols)} "
            f"FROM \"{table.name}\"{where}")
        copied = 0
        while True:
            chunk = cur.fetchmany(BATCH)
            if not chunk:
                break
            payload = [dict(zip(cols, row)) for row in chunk]
            async with engine.begin() as c:
                # ON CONFLICT DO NOTHING makes a re-run after a partial failure safe.
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                await c.execute(pg_insert(table).on_conflict_do_nothing(), payload)
            copied += len(chunk)
            print(f"    {table.name}: {copied}/{n}", end="\r", flush=True)
        print(f"  {table.name:<28} copied {copied}/{n}        ")

    await engine.dispose()
    src.close()
    if not args.apply and not args.verify:
        print("\ndry run — re-run with --apply")


asyncio.run(main())
```

- [ ] **Step 2: Dry run against the test database**

```bash
PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py \
  --pg-url postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  | tee /tmp/pg-migrate-dryrun.log
```
Expected: a row per table with `sqlite=<n> pg=0`. `ci_test_result` should report far fewer than
11,137,024 — roughly the failures plus the last 7 days.

- [ ] **Step 3: Apply to the TEST database first — never production**

```bash
PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --apply \
  --pg-url postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test \
  | tee /tmp/pg-migrate-test.log
```
Expected: every table reports `copied n/n`.

- [ ] **Step 4: Verify, then verify idempotency**

```bash
PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --verify \
  --pg-url postgresql+asyncpg://ccswitch_test:test@127.0.0.1:5432/ccswitch_test
```
Expected: every row `OK`. Then re-run Step 3 and Step 4 again — counts must be **unchanged**,
proving `on_conflict_do_nothing` makes a re-run safe.

- [ ] **Step 5: Confirm the SQLite file was not modified**

```bash
ls -la /home/tony/.claude-accounts/ccswitch.db
```
Expected: size and mtime unchanged from before Step 3. The script opens it `mode=ro`.

- [ ] **Step 6: Commit**

```bash
git add scripts/ccswitch-sqlite-to-pg.py
git commit -m "chore(db): add retention-scoped SQLite to Postgres migration script"
```

---

### Task 5: Cutover runbook

**Files:**
- Create: `docs/runbooks/postgres-cutover.md`

This task writes the runbook and **stops**. Executing the cutover mutates production and requires a
human decision — do not run it as part of implementing this plan.

- [ ] **Step 1: Write the runbook**

```markdown
# Postgres cutover runbook

Prerequisites: Tasks 1-4 merged and deployed; `ccswitch` role/database provisioned; a dry run
completed against production Postgres.

## Cutover

1. Back up SQLite:
   `cp -a /home/tony/.claude-accounts/ccswitch.db /mnt/hdd-storage/ccswitch.db.pre-pg`
2. Stop services (order matters — edge first, so nothing writes while the daemon drains):
   `sudo systemctl stop ccswitch.service ccswitch-deploy-controller.service ccswitch-daemon.service`
3. Confirm nothing is live: `systemctl is-active ccswitch.service ccswitch-daemon.service`
4. Migrate:
   `PYTHONPATH=<repo> .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --apply --pg-url "$PG_URL"`
5. Verify: same command with `--verify`. **Every table must read OK.** Stop here if any row differs.
6. Activate the URL: uncomment `CCSWITCH_DB_URL=postgresql+asyncpg://...` in
   `/etc/ccswitch/ccswitch.env` and comment out the sqlite one.
7. Start: `sudo systemctl start ccswitch-daemon.service ccswitch.service ccswitch-deploy-controller.service`
8. Verify live: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/api/health` → 200;
   dashboard lists sessions; `journalctl -u ccswitch-daemon.service -n 50` shows no DataError.

## Rollback

The SQLite file is untouched by the migration, so rollback is a config revert:

1. `sudo systemctl stop ccswitch.service ccswitch-deploy-controller.service ccswitch-daemon.service`
2. Restore the sqlite `CCSWITCH_DB_URL` line in `/etc/ccswitch/ccswitch.env`.
3. Start the services.

Any writes that landed in Postgres after cutover are lost on rollback — decide quickly, and treat
the first hour as the rollback window.

## Known gotchas

- A stale `/run/ccswitch/sessiond.sock` survives a stop (`RuntimeDirectoryPreserve=yes`) and trips
  the split-brain guard at `server/app.py:348`. Harmless for the real service, but any test run
  while stopped needs `CCSWITCH_SESSIOND_SOCKET=/nonexistent/...`.
- `init_db` runs `create_all` on every start; on Postgres it is a no-op once tables exist. The
  SQLite-only reconcile passes are correctly skipped by the `url.startswith("sqlite")` guard.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/postgres-cutover.md
git commit -m "docs: add Postgres cutover runbook"
```

---

## Verification

- [ ] `env CCSWITCH_TEST_PG_URL=... .venv/bin/python -m pytest server/tests/ -q` — green, with the
      Postgres tests running rather than skipping.
- [ ] `.venv/bin/python -m pytest server/tests/ -q` (no PG URL) — Postgres tests **skip**, suite green.
- [ ] `.venv/bin/python -m mypy server/ --ignore-missing-imports` — clean.
- [ ] `.venv/bin/python -m ruff check server/ scripts/` — clean.
- [ ] Migration script run twice against the test database yields identical counts.
- [ ] `/home/tony/.claude-accounts/ccswitch.db` size and mtime unchanged throughout.
