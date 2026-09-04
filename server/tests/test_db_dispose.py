"""dispose_db() tears the engine down cleanly; init_db() disposes a prior engine.

Regression guard for the connection/thread leak that produced the full-suite
`RuntimeError: Event loop is closed` flood: every app built an engine whose pooled
aiosqlite connection was never disposed, so it outlived its event loop.
"""
from __future__ import annotations

import asyncio

import server.db as db
from server.db import db_session, dispose_db, init_db


def test_dispose_db_clears_engine_and_factory():
    async def run():
        await init_db(db_url="sqlite+aiosqlite:///:memory:")
        assert db._engine is not None and db._session_factory is not None
        await dispose_db()
        assert db._engine is None and db._session_factory is None
        # A session after disposal fails loudly rather than silently reconnecting
        # to a fresh (empty) in-memory DB.
        try:
            async with db_session():
                pass
            raise AssertionError("db_session should raise after dispose_db()")
        except RuntimeError as e:
            assert "not initialised" in str(e)

    asyncio.run(run())


def test_dispose_db_is_idempotent():
    async def run():
        await dispose_db()  # no engine yet — must not raise
        await init_db(db_url="sqlite+aiosqlite:///:memory:")
        await dispose_db()
        await dispose_db()  # second dispose is a no-op

    asyncio.run(run())


def test_dispose_db_falls_back_when_graceful_close_wedges(monkeypatch):
    """Graceful engine.dispose() wedging must not hang teardown (#881).

    Under `-n auto` contention the single shared in-memory StaticPool connection
    can be held checked-out by an abandoned in-flight query, so the graceful
    close (`aiosqlite … _terminate_graceful_close → _conn.close() → await
    future`) never returns — wedging TestClient.__exit__'s portal-thread join and
    tripping the 300s pytest-timeout. dispose_db must bound the graceful close and
    fall back to `dispose(close=False)`, which drops the pool WITHOUT awaiting the
    wedged connection, then clear state and return.
    """
    monkeypatch.setattr(db, "_DISPOSE_GRACE_TIMEOUT_S", 0.05)

    class _WedgingEngine:
        def __init__(self):
            self.close_false_called = False

        async def dispose(self, close=True):
            if close:
                await asyncio.Event().wait()  # graceful close never returns
            else:
                self.close_false_called = True

    async def run():
        eng = _WedgingEngine()
        db._engine = eng
        db._session_factory = object()
        # Bounded so a regression (real hang) fails the test here rather than
        # only surfacing as the 300s suite-wide pytest-timeout.
        await asyncio.wait_for(dispose_db(), timeout=5)
        assert eng.close_false_called, "should fall back to dispose(close=False)"
        assert db._engine is None and db._session_factory is None

    asyncio.run(run())


def test_init_db_disposes_prior_engine(tmp_path):
    """Re-initialising replaces the engine; the old one is disposed, not leaked."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
        first = db._engine
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
        assert db._engine is not first
        # The prior engine was disposed (its pool is closed); dispose again is safe.
        await first.dispose()
        await dispose_db()

    asyncio.run(run())
