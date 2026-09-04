"""Regression guard for the test_ws.py ':memory:' teardown flake.

A ``sqlite+aiosqlite:///:memory:`` DB lives entirely inside one StaticPool
connection, so disposing the engine (app-lifespan shutdown) destroys the
schema. A background task that writes AFTER teardown — e.g. an orchestrator
persisting ``status='ended'`` — then reopens a fresh, EMPTY in-memory DB and
crashes with ``no such table: sessions``. That race made the WebSocket suite
flaky in CI (PRs #188/#192/#197/#200).

``conftest._install_durable_inmemory_db`` fixes it by redirecting every
in-memory URL to a temp file, which survives connection close. This test pins
that behaviour: a write issued after ``engine.dispose()`` must NOT raise. Drop
the shim and this test reproduces the original crash, so it can't silently
regress.

Issue #890 additionally pinned the shim to ``NullPool`` (not ``StaticPool``):
StaticPool holds ONE shared connection that any abandoned teardown phase can
wedge, hanging loop-close to the 300s pytest-timeout. NullPool keeps no pooled
connection, so there is no shared wedge-able substrate. The tests below pin
both the pool class and the cross-connection committed-visibility that NullPool
+ file + WAL must still provide.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool, StaticPool

import server.db as db


@pytest.mark.asyncio
async def test_late_write_after_dispose_does_not_lose_schema():
    await db.init_db(db_url="sqlite+aiosqlite:///:memory:")

    # Schema is present right after init.
    async with db.db_session() as session:
        await session.execute(
            text("UPDATE sessions SET status='running' WHERE id='nope'")
        )

    # Lifespan shutdown disposes the engine, closing pooled connections — the
    # exact moment a leaked background task may still be mid-write.
    await db._engine.dispose()

    # The late write must reconnect to a DB whose tables still exist. With a raw
    # ':memory:' StaticPool this raised OperationalError: no such table: sessions.
    async with db.db_session() as session:
        await session.execute(
            text("UPDATE sessions SET status='ended' WHERE id='nope'")
        )


@pytest.mark.asyncio
async def test_inmemory_shim_uses_nullpool_not_staticpool():
    """The shim must NOT pin StaticPool (issue #890).

    A StaticPool engine holds one shared connection that an abandoned bounded_wait
    teardown phase can leave wedged — the engine then hangs on awaiting its close
    at dispose / loop-close (the 300s teardown flake). NullPool retains no pooled
    connection, so there is nothing to wedge or await. Regressing the shim back to
    StaticPool re-arms the flake, so pin the pool class here where it fails loudly
    instead of only as a rare CI timeout under load.
    """
    await db.init_db(db_url="sqlite+aiosqlite:///:memory:")
    pool = db._engine.sync_engine.pool
    assert isinstance(pool, NullPool), f"expected NullPool, got {type(pool).__name__}"
    assert not isinstance(pool, StaticPool)


@pytest.mark.asyncio
async def test_committed_writes_visible_across_connections():
    """NullPool means separate connections per operation, so — unlike StaticPool's
    single shared connection — cross-session visibility now relies on the file +
    WAL + commit. db_session/get_db always commit, so a committed write must be
    readable from a subsequent (separate) connection."""
    from server.db.models import Session

    await db.init_db(db_url="sqlite+aiosqlite:///:memory:")

    # Insert through the ORM so Python-side column defaults are applied.
    async with db.db_session() as session:
        session.add(
            Session(
                id="s-890",
                name="t",
                account_name="acct",
                cwd="/tmp",
                status="running",
            )
        )  # db_session commits on exit

    # A fresh db_session() checks out a brand-new connection under NullPool; the
    # committed row must be visible there (file + WAL), proving the switch away
    # from StaticPool did not lose cross-connection consistency.
    async with db.db_session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM sessions WHERE id='s-890'")
            )
        ).scalar_one()
    assert status == "running"
