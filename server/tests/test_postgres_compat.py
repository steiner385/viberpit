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
        # Parent CiRun first so the ci_job.run_id FK is satisfied (Postgres
        # enforces FKs, SQLite does not).
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
