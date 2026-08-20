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
