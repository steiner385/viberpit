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
"""
import pytest
from sqlalchemy import text

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
