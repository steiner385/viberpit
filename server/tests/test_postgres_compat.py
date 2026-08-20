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
