import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CCSWITCH_TEST_PG_URL"),
    reason="set CCSWITCH_TEST_PG_URL to run Postgres-backed tests",
)


@pytest.mark.asyncio(loop_scope="session")
async def test_full_schema_builds_on_postgres(pg_engine):
    """Every mapped table must build on Postgres, not just SQLite."""
    from sqlalchemy import inspect as sa_inspect
    from server.db.models import Base

    async with pg_engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(sa_inspect(c).get_table_names()))

    expected = {t.name for t in Base.metadata.sorted_tables}
    missing = expected - names
    assert not missing, f"tables absent on Postgres: {sorted(missing)}"


@pytest.mark.asyncio(loop_scope="session")
async def test_utcnow_defaults_insert_and_query_on_postgres(pg_engine):
    """Every column defaulted with the AWARE _utcnow() must accept it.

    asyncpg raises DataError when an aware datetime is bound to a
    `timestamp without time zone` column — and it fails on comparisons too, not
    just inserts, so this exercises both.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import (Automation, AutomationEvent, AutomationTargetState,
                                  CiFinding, NotificationChannel, NotificationRule, _utcnow)

    Session = async_sessionmaker(pg_engine, expire_on_commit=False)
    # Required fields (NOT NULL, no default) are verified against the models:
    #   NotificationChannel.name/.url · AutomationEvent.summary · AutomationTargetState.target
    # Postgres enforces the automation_event/automation_target_state -> automation FK
    # (SQLite does not, by default), so a parent Automation row is required here too.
    rows = [
        NotificationChannel(id="t-chan", name="t-chan", kind="webhook",
                            url="https://example.invalid/hook"),
        NotificationRule(id="t-rule"),
        Automation(id="t-auto"),
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
        # Reverse insertion order so FK children (AutomationEvent,
        # AutomationTargetState) are deleted before their Automation parent.
        for r in reversed(rows):
            await s.delete(await s.merge(r))
        await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_ci_run_job_test_result_ids_survive_int64_on_postgres(pg_engine):
    """ci_run.id/ci_job.id/ci_job.run_id/ci_test_result.run_id hold GitHub
    run/job ids, which are int64 — e.g. run id 32407697884, job id
    96550614629. Postgres `integer` (32-bit) rejects those with
    "integer out of range"; SQLite accepted them silently because it is
    dynamically typed. Round-trip realistic out-of-int32-range ids through
    all three tables and confirm they come back exactly.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import CiJob, CiRun, CiTestResult

    RUN_ID = 32407697884
    JOB_ID = 96550614629
    TEST_RESULT_RUN_ID = 32404687323

    Session = async_sessionmaker(pg_engine, expire_on_commit=False)

    run = CiRun(id=RUN_ID, repo="o/r", conclusion="success")
    job = CiJob(id=JOB_ID, run_id=RUN_ID, repo="o/r", name="build")
    result = CiTestResult(run_id=TEST_RESULT_RUN_ID, repo="o/r",
                          suite="s", test_id="t1", outcome="passed")

    async with Session() as s:
        # Parent CiRun first, matching the natural insert order (CiJob.run_id
        # declares no ForeignKey — it's not FK-enforced on either engine — but
        # inserting the run before its jobs mirrors how the app writes them).
        s.add(run)
        await s.commit()
        s.add_all([job, result])
        await s.commit()
        await s.refresh(result)  # populate the autoincrement PK

    async with Session() as s:
        got_run = (await s.execute(select(CiRun).where(CiRun.id == RUN_ID))).scalar_one()
        got_job = (await s.execute(select(CiJob).where(CiJob.id == JOB_ID))).scalar_one()
        got_result = (await s.execute(
            select(CiTestResult).where(CiTestResult.id == result.id)
        )).scalar_one()

        assert got_run.id == RUN_ID
        assert got_job.id == JOB_ID
        assert got_job.run_id == RUN_ID
        assert got_result.run_id == TEST_RESULT_RUN_ID

    async with Session() as s:                # session-scoped fixture: clean up
        # Children (CiJob, CiTestResult) before the CiRun parent.
        await s.delete(await s.merge(job))
        await s.delete(await s.merge(result))
        await s.commit()
        await s.delete(await s.merge(run))
        await s.commit()


# --------------------------------------------------------------------------- #
# Metadata-driven DateTime coverage (fix-wave IMPORTANT 4)
#
# The row list above is fixed by hand — that's exactly how the earlier fix
# missed automation.last_active_at and ci_finding.resolved_at (Critical 1):
# both are written with an AWARE value by application code, but neither was
# in the hand-picked list, so nothing exercised them. This sweep instead
# walks Base.metadata.sorted_tables so a newly-added — or newly-missed —
# DateTime column cannot slip through silently: every DateTime column, on
# every mapped table, gets an explicit value of its declared awareness and
# must round-trip it.
# --------------------------------------------------------------------------- #

# Values for NOT NULL columns that have neither a default nor are one of the
# DateTime/FK columns handled generically below. Keyed by table name.
_REQUIRED_EXTRAS: dict[str, dict[str, object]] = {
    "sessions": {"name": "dtcheck", "account_name": "dtcheck", "cwd": "/tmp/dtcheck"},
    "app_sessions": {"identity": "dtcheck@example.invalid"},
    "audit_events": {"type": "dtcheck.event", "identity": "dtcheck@example.invalid"},
    "attachments": {"media_type": "image/png", "filename": "x.png", "byte_size": 1,
                     "sha256": "0" * 64, "uploaded_by": "dtcheck"},
    "session_queue": {"params": {}},
    "notification_channel": {"name": "dtcheck", "url": "https://example.invalid/hook"},
    "automation": {"id": "dtcheck-automation-sweep"},
    "automation_event": {"summary": "dtcheck"},
    "automation_target_state": {"target": "dtcheck-target"},
    "session_presets": {"name": "dtcheck-preset"},
    "prompt_templates": {"name": "dtcheck-template"},
    "permission_profiles": {"name": "dtcheck-profile"},
    # hosts.last_probe_at makes this table part of the sweep; name is the PK and
    # endpoint is NOT NULL, and neither has a synthesizable default (a host row
    # without an endpoint is unreachable by definition).
    "hosts": {"name": "dtcheck-host", "endpoint": "unix:///tmp/dtcheck.sock"},
}

# FK columns that must be filled from a parent row inserted earlier in this
# same sweep (Base.metadata.sorted_tables already orders parents first).
_FK_LINKS: dict[str, dict[str, str]] = {
    "session_queue": {"session_id": "sessions"},
    "automation_event": {"automation_id": "automation"},
    "automation_target_state": {"automation_id": "automation"},
    # initiative_artifacts joins the DateTime sweep via created_at; point its FK
    # at a real initiatives row (inserted earlier — sorted_tables orders parents
    # first) so the artifact references a live initiative rather than NULL.
    "initiative_artifacts": {"initiative_id": "initiatives"},
}


@pytest.mark.asyncio(loop_scope="session")
async def test_datetime_columns_round_trip_per_declared_awareness_on_postgres(pg_engine):
    """Every DateTime column on every mapped table must accept and return a
    value of its declared awareness (aware for timezone=True, naive
    otherwise). Metadata-driven so a future column can't silently repeat
    Critical 1 by being left out of a hand-maintained list."""
    from datetime import datetime, timezone

    from sqlalchemy import DateTime, delete as sa_delete, insert as sa_insert, select as sa_select
    from server.db.models import Base

    aware_dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    naive_dt = datetime(2024, 1, 2, 3, 4, 5)

    built_rows: dict[str, dict] = {}
    inserted_order: list = []

    async with pg_engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            dt_cols = [c for c in table.columns if isinstance(c.type, DateTime)]
            if not dt_cols:
                continue  # nothing to assert for this table

            row: dict[str, object] = dict(_REQUIRED_EXTRAS.get(table.name, {}))
            for col_name, parent_name in _FK_LINKS.get(table.name, {}).items():
                parent_pk = list(Base.metadata.tables[parent_name].primary_key.columns)[0].name
                row[col_name] = built_rows[parent_name][parent_pk]

            expected: dict[str, tuple[object, bool]] = {}
            for col in dt_cols:
                tz_aware = bool(getattr(col.type, "timezone", False))
                value = aware_dt if tz_aware else naive_dt
                row[col.name] = value
                expected[col.name] = (value, tz_aware)

            result = await conn.execute(
                sa_insert(table).values(**row).returning(*table.primary_key.columns))
            pk_row = result.one()
            for pk_col, pk_val in zip(table.primary_key.columns, pk_row):
                row[pk_col.name] = pk_val
            built_rows[table.name] = row
            inserted_order.append(table)

            got = (await conn.execute(
                sa_select(table).where(*[c == row[c.name] for c in table.primary_key.columns])
            )).mappings().one()
            for col_name, (value, tz_aware) in expected.items():
                got_val = got[col_name]
                assert got_val is not None, f"{table.name}.{col_name} round-tripped as NULL"
                assert (got_val.tzinfo is not None) == tz_aware, (
                    f"{table.name}.{col_name}: expected "
                    f"{'aware' if tz_aware else 'naive'} but got "
                    f"{'aware' if got_val.tzinfo is not None else 'naive'} "
                    f"(declared DateTime(timezone={tz_aware}) must match)")
                assert got_val.replace(tzinfo=None) == value.replace(tzinfo=None)

        # Clean up in reverse dependency order (children before parents) since
        # pg_engine is session-scoped and shared with every other test module.
        for table in reversed(inserted_order):
            row = built_rows[table.name]
            await conn.execute(sa_delete(table).where(
                *[c == row[c.name] for c in table.primary_key.columns]))


@pytest.mark.asyncio(loop_scope="session")
async def test_last_active_at_and_resolved_at_real_writer_paths_on_postgres(pg_engine, pg_url):
    """Exercise the ACTUAL production code paths that write the two Critical-1
    columns, not just a direct INSERT — that's exactly how the bug survived
    the earlier hand-picked-row test.

    automation.last_active_at is written inside
    ``server/sessions/orchestrator.py:_bump_automation_active``, wrapped in a
    bare ``except Exception: log.debug(...)`` — a naive column fails on
    Postgres SILENTLY, so the only way to catch a regression is to check the
    row actually changed, not that no exception propagated.

    ci_finding.resolved_at is written with the exact assignment
    ``row.status, row.resolved_at = "resolved", _utcnow()`` used in
    ``server/automations/cicd_doctor.py``, and is later compared against an
    aware cutoff (``resolved_at < finding_cutoff``) by the retention prune —
    both are reproduced here verbatim.
    """
    import contextlib
    from datetime import timedelta

    import server.db as db_module
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import Automation, CiFinding, _utcnow
    from server.sessions.orchestrator import SessionOrchestrator

    automation_id = "dtcheck-writer-automation"
    finding_id = "dtcheck-writer-finding"

    Session = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with Session() as s:
        s.add(Automation(id=automation_id))
        s.add(CiFinding(id=finding_id, repo="o/r", status="open"))
        await s.commit()

    # Point the module-level db_session() (what _bump_automation_active calls
    # internally) at this same Postgres database, then restore whatever was
    # there before — other tests in the suite may depend on the prior state.
    prev_engine, prev_factory = db_module._engine, db_module._session_factory
    await db_module.init_db(pg_url)
    try:
        # `self` is never referenced in _bump_automation_active's body, so
        # calling it unbound exercises the exact production coroutine without
        # standing up a full SessionOrchestrator (which needs a live
        # daemon/process environment this test does not have).
        await SessionOrchestrator._bump_automation_active(None, automation_id)
    finally:
        if db_module._engine is not None:
            with contextlib.suppress(Exception):
                await db_module._engine.dispose()
        db_module._engine, db_module._session_factory = prev_engine, prev_factory

    async with Session() as s:
        got = (await s.execute(
            select(Automation).where(Automation.id == automation_id))).scalar_one()
        assert got.last_active_at is not None, (
            "automation.last_active_at was never written — the bare "
            "`except Exception: log.debug(...)` in _bump_automation_active "
            "swallowed a failure. This is exactly how a naive column fails "
            "silently on Postgres.")
        assert got.last_active_at.tzinfo is not None

    async with Session() as s:
        row = (await s.execute(
            select(CiFinding).where(CiFinding.id == finding_id))).scalar_one()
        row.status, row.resolved_at = "resolved", _utcnow()
        await s.commit()

    finding_cutoff = _utcnow() + timedelta(days=1)  # future: the row must be "older"
    async with Session() as s:
        matched = (await s.execute(select(CiFinding).where(
            CiFinding.status == "resolved",
            CiFinding.resolved_at.is_not(None),
            CiFinding.resolved_at < finding_cutoff))).scalars().all()
        assert any(r.id == finding_id for r in matched), (
            "ci_finding.resolved_at did not compare correctly against an "
            "aware cutoff — this is the exact retention-pruning predicate "
            "from cicd_doctor.py")

    async with Session() as s:                # session-scoped fixture: clean up
        await s.execute(delete(CiFinding).where(CiFinding.id == finding_id))
        await s.execute(delete(Automation).where(Automation.id == automation_id))
        await s.commit()


# --------------------------------------------------------------------------- #
# dialect_insert() — the three ON CONFLICT call sites must work against the
# LIVE engine's dialect, not a hardcoded sqlite insert() (fix for the
# production AttributeError: 'OnConflictDoUpdate' object has no attribute
# 'constraint_target' — a sqlite Insert executed on Postgres).
# --------------------------------------------------------------------------- #

import contextlib as _contextlib
from contextlib import asynccontextmanager as _asynccontextmanager


@_asynccontextmanager
async def _pg_module_engine(pg_url):
    """Point server.db's module-level engine/session-factory at Postgres for
    the duration of the block, then restore whatever was there before —
    mirrors test_last_active_at_and_resolved_at_real_writer_paths_on_postgres
    above. Needed because upsert_merged_pr/record_group_outcome/
    deploy_config_put call db_session()/dialect_insert() against the module
    singletons, not an injected engine."""
    import server.db as db_module

    prev_engine, prev_factory = db_module._engine, db_module._session_factory
    await db_module.init_db(pg_url)
    try:
        yield
    finally:
        if db_module._engine is not None:
            with _contextlib.suppress(Exception):
                await db_module._engine.dispose()
        db_module._engine, db_module._session_factory = prev_engine, prev_factory


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_merged_pr_first_value_wins_round_trip_on_postgres(pg_engine, pg_url):
    """The bug that's firing in production right now: upsert_merged_pr built
    its ON CONFLICT statement with sqlalchemy.dialects.sqlite.insert(), which
    raises AttributeError on a Postgres engine. Round-trip the exact
    COALESCE(existing, excluded) 'first non-NULL write wins' semantics the
    store documents, against a real Postgres connection.
    """
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import MergedPr
    from server.pipeline.timeline import upsert_merged_pr

    repo, number = "o/r", 9001
    async with _pg_module_engine(pg_url):
        # Insert leaves first_green_at/qa_live_at NULL.
        await upsert_merged_pr(
            repo, number, merged_at="2026-07-12T00:00:00Z", title="orig",
            first_green_at=None,
        )
        # Second upsert supplies first_green_at (NULL -> fills) and a
        # different title (already set -> must be ignored) — this is the
        # on_conflict_do_update path that raised in production.
        await upsert_merged_pr(
            repo, number, merged_at="2026-07-12T09:00:00Z", title="changed",
            first_green_at="2026-07-11T12:00:00Z",
        )

        Session = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with Session() as s:
            got = await s.get(MergedPr, (repo, number))
            assert got is not None
            assert got.title == "orig", "first-set value must win over the later write"
            assert got.first_green_at == "2026-07-11T12:00:00Z", (
                "NULL column must adopt the later write")
            assert got.merged_at == "2026-07-12T00:00:00Z", (
                "merged_at is a writer-side invariant — never overwritten on conflict")

        async with Session() as s:            # session-scoped fixture: clean up
            await s.execute(delete(MergedPr).where(
                MergedPr.repo == repo, MergedPr.number == number))
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_record_group_outcome_upsert_and_dedupe_on_postgres(pg_engine, pg_url):
    """group_history's two on_conflict_do_nothing sites (GroupRun dedup on
    (repo, completed_at), GroupFailure dedup on (repo, group_sha, check_name))
    must execute — and actually dedupe — on Postgres."""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import GroupFailure, GroupRun
    from server.pipeline.group_history import record_group_outcome

    repo = "o/r"
    green_checks = [
        {"name": "build", "conclusion": "SUCCESS",
         "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:05:00Z"},
    ]
    failing_checks = [
        {"name": "lint", "conclusion": "FAILURE",
         "started_at": "2026-07-12T13:00:00Z", "completed_at": "2026-07-12T13:05:00Z"},
    ]

    async with _pg_module_engine(pg_url):
        # Record the same clean run twice — the second call must hit
        # on_conflict_do_nothing rather than raise a unique-constraint error
        # or the sqlite-dialect AttributeError.
        await record_group_outcome(repo, "sha-green", green_checks, "2026-07-12T12:10:00Z")
        await record_group_outcome(repo, "sha-green", green_checks, "2026-07-12T12:10:00Z")

        await record_group_outcome(repo, "sha-fail", failing_checks, "2026-07-12T13:10:00Z")
        await record_group_outcome(repo, "sha-fail", failing_checks, "2026-07-12T13:10:00Z")

        Session = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with Session() as s:
            runs = (await s.execute(
                select(GroupRun).where(GroupRun.repo == repo))).scalars().all()
            failures = (await s.execute(
                select(GroupFailure).where(GroupFailure.repo == repo))).scalars().all()
            assert len(runs) == 1, "duplicate completed_at must be deduped, not double-inserted"
            assert len(failures) == 1, (
                "duplicate (repo, group_sha, check_name) must be deduped")

        async with Session() as s:            # session-scoped fixture: clean up
            await s.execute(delete(GroupRun).where(GroupRun.repo == repo))
            await s.execute(delete(GroupFailure).where(GroupFailure.repo == repo))
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_deploy_config_put_upsert_on_postgres(pg_engine, pg_url):
    """orchestrator.deploy_config_put's on_conflict_do_update (overwrite
    semantics, not COALESCE) must execute on Postgres."""
    import json
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.db.models import RepoDeployConfig
    from server.sessions.orchestrator import SessionOrchestrator

    repo = "o/r-deploy-cfg"
    async with _pg_module_engine(pg_url):
        # `self` is unused in deploy_config_put's body (it only closes over
        # the module-level db_session()), so call it unbound like the
        # _bump_automation_active precedent above.
        await SessionOrchestrator.deploy_config_put(
            None, repo, [{"name": "qa", "healthUrl": "https://example.invalid"}], True)
        # Second call on the SAME repo exercises on_conflict_do_update with
        # different values — must overwrite, not raise.
        await SessionOrchestrator.deploy_config_put(
            None, repo, [{"name": "prod", "healthUrl": "https://example.invalid/p"}], False)

        Session = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with Session() as s:
            got = await s.get(RepoDeployConfig, repo)
            assert got is not None
            assert json.loads(got.environments) == [
                {"name": "prod", "healthUrl": "https://example.invalid/p"}]
            assert got.enabled is False

        async with Session() as s:            # session-scoped fixture: clean up
            await s.execute(delete(RepoDeployConfig).where(RepoDeployConfig.repo == repo))
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_reconcile_adds_missing_column_on_postgres(pg_engine, pg_url):
    """A mapped column absent from an existing Postgres table must be ALTERed in.

    The reconcile used to be SQLite-gated (`if url.startswith("sqlite")` in
    init_db), which was correct while the live deploy was SQLite. After the
    Postgres cutover nothing created newly added model columns: the deploy went
    green and then every query selecting that column failed at runtime with
    UndefinedColumnError. Reconcile is dialect-generic — it compiles the column
    type against the live dialect — so it must run here too.

    Self-restoring: drops a nullable column, then reconciles it straight back.
    """
    from sqlalchemy import inspect as sa_inspect, text as sa_text

    from server.db import dispose_db, init_db

    col = "archive_root"

    def cols(c):
        return {x["name"] for x in sa_inspect(c).get_columns("sessions")}

    async with pg_engine.begin() as conn:
        await conn.execute(sa_text(f'ALTER TABLE sessions DROP COLUMN IF EXISTS "{col}"'))
    async with pg_engine.connect() as conn:
        assert col not in await conn.run_sync(cols), "precondition: column should be gone"

    # Drive the REAL startup path, not _reconcile_columns directly: the bug was not
    # that reconcile failed on Postgres, it was that init_db never called it there.
    try:
        await init_db(db_url=pg_url)
    finally:
        await dispose_db()

    async with pg_engine.connect() as conn:
        assert col in await conn.run_sync(cols), (
            "init_db must reconcile a missing model column on Postgres, not only on SQLite")
