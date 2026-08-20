# CI Retention + App-Session Prune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the ~1 GB/day growth of `ccswitch.db` by aggregating passed CI test results, deduping
JUnit ingestion, and ending per-request `app_sessions` minting — shrinking the live DB from 7.8 GB to
a bounded size before the Postgres migration.

**Architecture:** Three independent leaks are fixed at their source, then one throwaway script cleans
the existing backlog. Ingestion dedupes repeated `<testcase>` entries before insert. The CI Doctor's
existing `_prune` gains a rollup step that folds raw `passed`/`skipped` rows older than
`raw_retention_days` into per-(repo, test, day) counts in a new `ci_test_daily` table, keeping
`failed` rows raw. The auth middleware reuses an identity's newest live session instead of inserting
a row whenever a request arrives without a usable cookie.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (async) + aiosqlite, FastAPI, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` (sub-project 1;
this plan is phase **1a**, the prerequisite to the Postgres migration in 1b).

## Global Constraints

- **TDD (Principle V).** Every task writes a failing test first, watches it fail, then implements.
- **New model columns MUST be nullable.** `create_all` never ALTERs an existing table; `init_db`
  backfills only *nullable* columns via `_reconcile_sqlite_columns`. A NOT NULL column needs a real
  migration. This applies to every column added in Task 2.
- **New code must be dialect-portable.** Phase 1b moves this DB to Postgres. Do date bucketing in
  Python, never in SQL — no `strftime`, no `datetime('now')`, no SQLite-only syntax.
- **Single source of truth (Principle VI).** Retention logic lives in `CicdDoctorLoop._prune` only;
  do not add a second prune path. Delete replaced code in the same commit.
- **Test command:** `.venv/bin/python -m pytest server/tests/ -q`
- **Auth tests 401 unless the identity is allowlisted.** Run with
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest …`, or pin
  `CCSWITCH_TAILNET_ALLOWLIST` per test.
- **Services are currently stopped.** Restart with
  `sudo systemctl start ccswitch-daemon.service ccswitch.service ccswitch-deploy-controller.service`.
  Task 5 requires them stopped.

## Measured baseline (dobby, 2026-08-20)

| Fact | Value |
|---|---|
| `ccswitch.db` / WAL | 7.8 GB / 767 MB |
| `ci_test_result` rows | 11,845,027 (`passed` 11,608,090 · `skipped` 236,395 · `failed` **542**) |
| … written in last 7 days | 11,567,799 (~1.65M/day) |
| … true duplicates by `(run_id, suite, test_id)` | 430,779 (3.6%) |
| `app_sessions` rows | 2,204,240 — **2,162,193 expired**, 42,047 live |
| … minted per day | ~45,000, of which `tonystein85@gmail.com` = 2,201,584 total |
| Existing `retention_days` default | **90** (never bites: only 11,010 rows exceed 30 days) |

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `server/db/models.py` | ORM schema | Modify: add `CiTestDaily`; add unique constraint to `CiTestResult` |
| `server/automations/cicd_doctor.py` | CI ingest / analyze / prune | Modify: dedupe in `_ingest`; add `_rollup_passed`; call it from `_prune`; add `raw_retention_days` config field |
| `server/auth/middleware.py` | Identity → app session | Modify: reuse newest live session before creating |
| `scripts/ccswitch-db-compact.py` | One-shot backlog cleanup | Create (throwaway, documented as such) |
| `server/tests/test_cicd_retention.py` | Rollup + dedupe coverage | Create |
| `server/tests/test_auth_session_reuse.py` | Session-reuse coverage | Create |

---

### Task 1: Dedupe repeated JUnit testcases at ingest

A JUnit XML emits one `<testcase>` per attempt, so a framework retry writes the same
`(run_id, suite, test_id)` several times — 430,779 rows today, one case repeated 6×. Keep the **last**
attempt: it is the outcome the run finished with.

**Files:**
- Modify: `server/automations/cicd_doctor.py:166-176` (the `for c in cases:` loop inside `_ingest`)
- Modify: `server/db/models.py:504-517` (`CiTestResult`)
- Test: `server/tests/test_cicd_retention.py`

**Interfaces:**
- Consumes: `parse_junit_xml(xml) -> list[TestCase]` from `server/automations/cicd/junit.py`; each
  case exposes `.test_id`, `.outcome`, `.time_s`, `.message`.
- Produces: no new public symbol. `CiTestResult` gains
  `UniqueConstraint("run_id", "suite", "test_id", name="uq_ci_test_result_run_suite_test")`,
  which Task 5 creates on the live DB.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_cicd_retention.py
import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import CiTestResult


@pytest.mark.asyncio
async def test_repeated_testcase_keeps_last_attempt(monkeypatch):
    """A retried test emits one <testcase> per attempt; only the final one is stored."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.automations.cicd_doctor import CicdDoctorLoop

    xml = """<testsuites><testsuite name="s">
      <testcase classname="c" name="flaky" time="0.1"><failure>boom</failure></testcase>
      <testcase classname="c" name="flaky" time="0.2"/>
    </testsuite></testsuites>"""

    engine = CicdDoctorLoop.__new__(CicdDoctorLoop)
    engine._fetch_runs = lambda repo, since: _runs()
    engine._fetch_jobs = lambda repo, run_id: _none()
    engine._fetch_junit = lambda repo, run_id: _junit(xml)
    engine._ctx = _FakeCtx()

    await engine._ingest("owner/repo")

    async with db_session() as db:
        rows = (await db.execute(select(CiTestResult))).scalars().all()
    assert len(rows) == 1, f"expected one deduped row, got {len(rows)}"
    assert rows[0].outcome == "passed"  # the LAST attempt won


async def _runs():
    return [{"id": 1, "workflow_path": "ci.yml", "event": "push", "head_sha": "abc",
             "head_branch": "main", "conclusion": "success", "created_ts": 1000.0,
             "duration_s": 1.0}]


async def _none():
    return []


async def _junit(xml):
    return [("suite-1", xml)]


class _FakeCtx:
    async def get_target_state(self, key):
        return {}

    async def set_target_state(self, key, value):
        return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py::test_repeated_testcase_keeps_last_attempt -q`
Expected: FAIL — `assert 2 == 1`, two rows stored.

- [ ] **Step 3: Dedupe in `_ingest`**

In `server/automations/cicd_doctor.py`, replace the `for c in cases:` block with:

```python
                    # One <testcase> per ATTEMPT: a framework retry repeats the same
                    # (suite, test_id). Keep the LAST attempt — it is the outcome the
                    # run finished with — so ingestion is idempotent against
                    # uq_ci_test_result_run_suite_test. 430,779 rows (3.6% of the
                    # table) were duplicates before this.
                    last_attempt: dict[str, object] = {}
                    for c in cases:
                        last_attempt[c.test_id] = c
                    for c in last_attempt.values():
                        db.add(CiTestResult(repo=repo, run_id=r["id"], suite=suite,
                                            test_id=c.test_id, outcome=c.outcome,
                                            time_s=c.time_s, message=c.message,
                                            head_sha=r["head_sha"],
                                            head_branch=r["head_branch"],
                                            created_ts=r["created_ts"]))
```

- [ ] **Step 4: Add the unique constraint to the model**

In `server/db/models.py`, add the import and the `__table_args__` to `CiTestResult`:

```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
```

```python
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    # Backstop for the ingest-side dedupe above. NULLs compare as distinct in SQL, so
    # rows with a NULL suite/test_id are NOT covered — ingest-side dedupe is the
    # primary guard, this catches concurrent-tick races. Created on the live DB by
    # scripts/ccswitch-db-compact.py (create_all does not add constraints to an
    # existing table).
    __table_args__ = (
        UniqueConstraint("run_id", "suite", "test_id",
                         name="uq_ci_test_result_run_suite_test"),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py server/tests/test_cicd_junit.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/automations/cicd_doctor.py server/db/models.py server/tests/test_cicd_retention.py
git commit -m "fix(cicd): dedupe repeated JUnit testcases at ingest"
```

---

### Task 2: Add the `ci_test_daily` rollup table

**Files:**
- Modify: `server/db/models.py` (add after `CiTestResult`)
- Test: `server/tests/test_cicd_retention.py`

**Interfaces:**
- Produces: `CiTestDaily` with columns `id, repo, test_id, day, passed, failed, skipped` and
  `UniqueConstraint("repo", "test_id", "day", name="uq_ci_test_daily")`. Task 3 writes it; the
  flaky-test analyzers read it. `day` is a `'YYYY-MM-DD'` UTC string — a string, not a date, so the
  same value round-trips identically on SQLite and Postgres.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_cicd_retention.py
@pytest.mark.asyncio
async def test_ci_test_daily_table_exists():
    """The rollup table is created by create_all and accepts a day-count row."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.db.models import CiTestDaily

    async with db_session() as db:
        db.add(CiTestDaily(repo="o/r", test_id="t1", day="2026-08-20",
                           passed=5, failed=1, skipped=0))
    async with db_session() as db:
        row = (await db.execute(select(CiTestDaily))).scalars().one()
    assert (row.passed, row.failed, row.skipped) == (5, 1, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py::test_ci_test_daily_table_exists -q`
Expected: FAIL — `ImportError: cannot import name 'CiTestDaily'`

- [ ] **Step 3: Add the model**

```python
class CiTestDaily(Base):
    """Per-(repo, test, day) outcome counts, rolled up from CiTestResult.

    Flaky-test detection needs the RATE at which a test fails, not each individual
    passing row. Raw passed/skipped rows older than raw_retention_days are folded in
    here and then dropped (CicdDoctorLoop._rollup_passed); failed rows stay raw for
    retention_days because findings quote their message.

    Every non-key column is NULLABLE — the SQLite reconcile rule (see
    test_schema_reconcile_coverage): init_db can only backfill nullable columns.
    """

    __tablename__ = "ci_test_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    test_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # 'YYYY-MM-DD' UTC. A string, not a Date: it is bucketed in Python so the value is
    # identical on SQLite and Postgres (see the plan's portability constraint).
    day: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    passed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    skipped: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    __table_args__ = (
        UniqueConstraint("repo", "test_id", "day", name="uq_ci_test_daily"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py -q`
Expected: PASS

- [ ] **Step 5: Verify the reconcile-coverage guard still passes**

Run: `.venv/bin/python -m pytest server/tests/ -q -k "schema_reconcile or drift"`
Expected: PASS — every new column is nullable, so reconcile can add them to an existing DB.

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/tests/test_cicd_retention.py
git commit -m "feat(cicd): add ci_test_daily rollup table"
```

---

### Task 3: Roll up passed results and prune raw rows

**Files:**
- Modify: `server/automations/cicd_doctor.py:345-366` (`_prune`), plus a new `_rollup_passed`
- Modify: `server/automations/cicd_doctor.py` `config_schema` (add `raw_retention_days`)
- Test: `server/tests/test_cicd_retention.py`

**Interfaces:**
- Consumes: `CiTestDaily` (Task 2); `self._clock() -> float` (epoch seconds).
- Produces: `CicdDoctorLoop._rollup_passed(config: dict) -> None`, called from `_prune` **before**
  the existing age-based deletes so a row is always counted before it can be removed.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_cicd_retention.py
@pytest.mark.asyncio
async def test_rollup_folds_passed_and_keeps_failed():
    """Passed rows past the raw window become day-counts; failed rows stay raw."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.automations.cicd_doctor import CicdDoctorLoop
    from server.db.models import CiTestDaily

    now = 1_800_000_000.0
    old = now - 10 * 86400.0          # older than the 7-day raw window
    async with db_session() as db:
        for _ in range(3):
            db.add(CiTestResult(repo="o/r", run_id=1, suite="s", test_id="t1",
                                outcome="passed", created_ts=old))
        db.add(CiTestResult(repo="o/r", run_id=1, suite="s", test_id="t2",
                            outcome="failed", message="boom", created_ts=old))

    engine = CicdDoctorLoop.__new__(CicdDoctorLoop)
    engine._clock = lambda: now
    await engine._rollup_passed({})

    async with db_session() as db:
        raw = (await db.execute(select(CiTestResult))).scalars().all()
        daily = (await db.execute(select(CiTestDaily))).scalars().all()

    assert [r.outcome for r in raw] == ["failed"], "failed rows must stay raw"
    assert len(daily) == 1 and daily[0].passed == 3
    assert daily[0].test_id == "t1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py::test_rollup_folds_passed_and_keeps_failed -q`
Expected: FAIL — `AttributeError: 'CicdDoctorLoop' object has no attribute '_rollup_passed'`

- [ ] **Step 3: Implement the rollup**

Add to `CicdDoctorLoop` in `server/automations/cicd_doctor.py`:

```python
# Rows folded per tick. Bounds the transaction so a tick never rewrites millions of
# rows at once; the backlog is drained by scripts/ccswitch-db-compact.py.
_ROLLUP_BATCH = 20_000


    async def _rollup_passed(self, config: dict) -> None:
        """Fold raw passed/skipped rows older than raw_retention_days into
        CiTestDaily day-counts, then delete them. Failed rows are never folded —
        findings quote their message, so they stay raw until retention_days.

        Day bucketing happens in PYTHON, not SQL: this must behave identically on
        SQLite today and Postgres after sub-project 1b.
        """
        from collections import Counter
        from datetime import datetime, timezone

        from sqlalchemy import delete, select
        from server.db import db_session
        from server.db.models import CiTestDaily, CiTestResult

        raw_days = float(config.get("raw_retention_days", 7))
        horizon = self._clock() - raw_days * 86400.0
        async with db_session() as db:
            rows = (await db.execute(
                select(CiTestResult.id, CiTestResult.repo, CiTestResult.test_id,
                       CiTestResult.outcome, CiTestResult.created_ts)
                .where(CiTestResult.outcome != "failed",
                       CiTestResult.created_ts < horizon)
                .limit(_ROLLUP_BATCH))).all()
            if not rows:
                return
            counts: Counter = Counter()
            for _id, repo, test_id, outcome, ts in rows:
                day = datetime.fromtimestamp(ts or 0.0, timezone.utc).strftime("%Y-%m-%d")
                counts[(repo, test_id, day, outcome)] += 1
            for (repo, test_id, day, outcome), n in counts.items():
                bucket = (await db.execute(select(CiTestDaily).where(
                    CiTestDaily.repo == repo,
                    CiTestDaily.test_id == test_id,
                    CiTestDaily.day == day))).scalar_one_or_none()
                if bucket is None:
                    bucket = CiTestDaily(repo=repo, test_id=test_id, day=day,
                                         passed=0, failed=0, skipped=0)
                    db.add(bucket)
                    await db.flush()
                if outcome == "skipped":
                    bucket.skipped = (bucket.skipped or 0) + n
                else:
                    bucket.passed = (bucket.passed or 0) + n
            await db.execute(delete(CiTestResult).where(
                CiTestResult.id.in_([r[0] for r in rows])))
```

- [ ] **Step 4: Call it from `_prune`**

In `_prune`, immediately after the docstring and before the existing deletes:

```python
        # Fold passed/skipped into day-counts BEFORE the age-based deletes, so a row
        # is always counted before it can be dropped.
        await self._rollup_passed(config)
```

- [ ] **Step 5: Add the config field**

In the `CicdDoctor` automation's `config_schema`, add:

```python
        ConfigField(key="raw_retention_days", label="Raw test-result window (days)",
                    type="int", default=7,
                    help="Keep individual passed/skipped test rows this long, then fold "
                         "them into per-day counts. Failed rows are kept raw for the "
                         "full retention window."),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_cicd_retention.py server/tests/test_cicd_doctor_automation.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add server/automations/cicd_doctor.py server/tests/test_cicd_retention.py
git commit -m "feat(cicd): roll passed test results into daily counts"
```

---

### Task 4: Stop minting an app_session per cookie-less request

`_resolve_or_create_app_session` inserts a row whenever a request arrives without a usable cookie — 45,000/day for
one identity, 2.2M rows total. Reuse that identity's newest live session instead.

**Files:**
- Modify: `server/auth/middleware.py:170-179`
- Test: `server/tests/test_auth_session_reuse.py`

**Interfaces:**
- Consumes: `AppSession` (`server/db/models.py:187`); the signature is
  `_resolve_or_create_app_session(request, db, identity)` — note db precedes identity.
- Produces: no signature change — `_resolve_or_create_app_session` keeps its return type `AppSession`.

**Security note for the reviewer:** the reuse lookup is filtered by `AppSession.identity == identity`,
and `identity` is verified by the Tailscale middleware *before* this function runs. It therefore
returns a row the same principal already owns and never widens access across identities. Confirm
that filter is present when reviewing.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_auth_session_reuse.py
import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AppSession


@pytest.mark.asyncio
async def test_cookieless_requests_reuse_one_session():
    """Ten cookie-less requests for one identity must not mint ten rows."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    class _Req:
        cookies: dict = {}

    for _ in range(10):
        async with db_session() as db:
            await _resolve_or_create_app_session(_Req(), db, "user@example.com")

    async with db_session() as db:
        rows = (await db.execute(select(AppSession))).scalars().all()
    assert len(rows) == 1, f"expected reuse, minted {len(rows)} rows"


@pytest.mark.asyncio
async def test_distinct_identities_do_not_share_a_session():
    """Reuse is scoped to the identity — never across principals."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    class _Req:
        cookies: dict = {}

    async with db_session() as db:
        a = await _resolve_or_create_app_session(_Req(), db, "a@example.com")
        b = await _resolve_or_create_app_session(_Req(), db, "b@example.com")
    assert a.id != b.id
    assert (a.identity, b.identity) == ("a@example.com", "b@example.com")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_auth_session_reuse.py -q`
Expected: FAIL — `expected reuse, minted 10 rows`

- [ ] **Step 3: Reuse before creating**

In `server/auth/middleware.py`, immediately before the `# Create new` block:

```python
    # No usable cookie: reuse this identity's newest live session rather than
    # inserting a row. Every cookie-less caller (API polls, WS upgrades) previously
    # minted one — 45k rows/day for a single identity, 2.2M rows total.
    # Scoped to `identity`, which the Tailscale middleware has already verified, so
    # this only ever returns a row the same principal owns.
    reusable = (await db.execute(
        select(AppSession)
        .where(
            AppSession.identity == identity,
            AppSession.revoked_at.is_(None),
            AppSession.expires_at > now,
        )
        .order_by(AppSession.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if reusable is not None:
        return reusable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_auth_session_reuse.py server/tests/ -q -k "auth or session"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/auth/middleware.py server/tests/test_auth_session_reuse.py
git commit -m "fix(auth): reuse live app_session instead of minting per request"
```

---

### Task 5: One-shot compaction of the existing 7.8 GB database

Tasks 1–4 stop the leaks; this drains the backlog. **Throwaway script** — it runs once, is not
imported by the app, and is deleted once the migration in 1b completes.

**Files:**
- Create: `scripts/ccswitch-db-compact.py`

**Interfaces:**
- Consumes: nothing from the app. Operates on the SQLite file directly via `sqlite3` so it can run
  with the services stopped and without loading the server package.

**Preconditions — verify before running:**

```bash
systemctl is-active ccswitch.service ccswitch-daemon.service   # must print inactive twice
cp -a /home/tony/.claude-accounts/ccswitch.db /mnt/hdd-storage/ccswitch.db.pre-compact
```

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""ONE-SHOT compaction of ccswitch.db. Throwaway — delete after sub-project 1b.

Drains the backlog the leak fixes (Tasks 1-4) prevent from recurring:
  1. delete expired/revoked app_sessions      (~2.16M rows)
  2. dedupe ci_test_result by (run_id, suite, test_id), keeping the highest id
  3. fold passed/skipped rows older than --raw-days into ci_test_daily
  4. create the unique indexes create_all cannot add to an existing table
  5. VACUUM to return the freed pages to the filesystem

Run ONLY with ccswitch.service and ccswitch-daemon.service stopped.
"""
import argparse
import sqlite3
import time

DB = "/home/tony/.claude-accounts/ccswitch.db"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--raw-days", type=float, default=7.0)
    ap.add_argument("--apply", action="store_true",
                    help="without this, only report what would change")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=30000")
    horizon = time.time() - args.raw_days * 86400.0

    def count(sql: str, *p) -> int:
        return con.execute(sql, p).fetchone()[0]

    print("expired app_sessions :", count(
        "SELECT COUNT(*) FROM app_sessions "
        "WHERE revoked_at IS NOT NULL OR expires_at < datetime('now')"))
    print("duplicate test rows  :", count(
        "SELECT COUNT(*) - COUNT(DISTINCT run_id||'|'||COALESCE(suite,'')||'|'||test_id) "
        "FROM ci_test_result"))
    print("foldable test rows   :", count(
        "SELECT COUNT(*) FROM ci_test_result "
        "WHERE outcome != 'failed' AND created_ts < ?", horizon))
    if not args.apply:
        print("\ndry run — re-run with --apply")
        return

    con.execute("DELETE FROM app_sessions "
                "WHERE revoked_at IS NOT NULL OR expires_at < datetime('now')")
    con.commit()

    # Keep the highest id per (run_id, suite, test_id) — ingest appends, so the
    # highest id is the last attempt, matching Task 1's keep-last rule.
    con.execute("""
        DELETE FROM ci_test_result WHERE id NOT IN (
            SELECT MAX(id) FROM ci_test_result
            GROUP BY run_id, COALESCE(suite,''), test_id)
    """)
    con.commit()

    con.execute("""
        CREATE TABLE IF NOT EXISTS ci_test_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo VARCHAR, test_id VARCHAR, day VARCHAR,
            passed INTEGER, failed INTEGER, skipped INTEGER)
    """)
    con.execute("""
        INSERT INTO ci_test_daily (repo, test_id, day, passed, failed, skipped)
        SELECT repo, test_id, strftime('%Y-%m-%d', created_ts, 'unixepoch'),
               SUM(outcome = 'passed'), 0, SUM(outcome = 'skipped')
        FROM ci_test_result
        WHERE outcome != 'failed' AND created_ts < ?
        GROUP BY repo, test_id, strftime('%Y-%m-%d', created_ts, 'unixepoch')
    """, (horizon,))
    con.execute("DELETE FROM ci_test_result "
                "WHERE outcome != 'failed' AND created_ts < ?", (horizon,))
    con.commit()

    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ci_test_result_run_suite_test "
                "ON ci_test_result (run_id, suite, test_id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ci_test_daily "
                "ON ci_test_daily (repo, test_id, day)")
    con.commit()

    print("vacuuming (needs free space equal to the final DB size)…")
    con.execute("VACUUM")
    con.close()
    print("done")


if __name__ == "__main__":
    main()
```

> `strftime(...,'unixepoch')` here is deliberate and safe: this script targets the SQLite file
> directly and is deleted after 1b. The **application** code in Task 3 buckets in Python, per the
> portability constraint.

- [ ] **Step 2: Dry run and record the numbers**

```bash
.venv/bin/python scripts/ccswitch-db-compact.py | tee /tmp/ccswitch-compact-dryrun.log
```
Expected: three non-zero counts, roughly matching the measured baseline table above.

- [ ] **Step 3: Back up, then apply**

```bash
cp -a /home/tony/.claude-accounts/ccswitch.db /mnt/hdd-storage/ccswitch.db.pre-compact
.venv/bin/python scripts/ccswitch-db-compact.py --apply | tee /tmp/ccswitch-compact.log
ls -la /home/tony/.claude-accounts/ccswitch.db
```
Expected: final file **well under 1 GB** (from 7.8 GB).

- [ ] **Step 4: Restart and verify the app reads the compacted DB**

```bash
sudo systemctl start ccswitch-daemon.service ccswitch.service ccswitch-deploy-controller.service
systemctl is-active ccswitch.service ccswitch-daemon.service
journalctl -u ccswitch.service -n 50 --no-pager | grep -i "error\|drift" || echo "clean"
```
Expected: both active, no `SchemaDriftError`, dashboard loads and CI findings still render.

- [ ] **Step 5: Commit**

```bash
git add scripts/ccswitch-db-compact.py
git commit -m "chore(db): one-shot compaction script for the ci/app_session backlog"
```

---

## Verification

- [ ] `.venv/bin/python -m pytest server/tests/ -q` — full suite green.
- [ ] `.venv/bin/python -m ruff check server/ scripts/` — lint clean.
- [ ] `ls -la /home/tony/.claude-accounts/ccswitch.db` — under 1 GB.
- [ ] After 24 h: `SELECT COUNT(*) FROM app_sessions;` grows by single digits, not ~45,000.
- [ ] After 24 h: `SELECT COUNT(*) FROM ci_test_result;` is bounded near the 7-day raw window
      instead of climbing by ~1.65M/day.
