"""session_queue table + status SoT + max_active_sessions setting."""
import asyncio


def test_non_active_statuses_superset():
    from server.db.models import (DEFAULT_MAX_ACTIVE_SESSIONS,
                                  INACTIVE_SESSION_STATUSES,
                                  NON_ACTIVE_SESSION_STATUSES)
    assert INACTIVE_SESSION_STATUSES == frozenset({"ended", "archived"})
    assert NON_ACTIVE_SESSION_STATUSES == INACTIVE_SESSION_STATUSES | {"queued"}
    assert DEFAULT_MAX_ACTIVE_SESSIONS == 5


def test_session_queue_roundtrip(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
        from datetime import datetime, timezone
        from sqlalchemy import select
        from server.db.models import SessionQueue
        async with db_session() as db:
            db.add(SessionQueue(session_id="s1", priority=1,
                                enqueued_at=datetime.now(timezone.utc),
                                params={"initial_prompt": "hi", "cols": 80}))
        async with db_session() as db:
            row = (await db.execute(select(SessionQueue))).scalar_one()
            assert row.session_id == "s1"
            assert row.params["initial_prompt"] == "hi"

    asyncio.run(run())


def test_app_settings_max_active_nullable(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        from server.db.models import AppSettings
        async with db_session() as db:
            db.add(AppSettings(id="singleton"))
        from sqlalchemy import select
        async with db_session() as db:
            s = (await db.execute(select(AppSettings))).scalar_one()
            assert s.max_active_sessions is None  # NULL → default 5 applies

    asyncio.run(run())


def test_next_entry_exclude_advances_past_head(tmp_path):
    """next_entry(exclude=...) skips excluded ids and returns the next in order;
    excluding everything returns None. This powers the drain loop's same-pass
    guard AND its account-gated skip (leave head queued, try the next entry)."""
    from server.db import init_db
    from server.sessions import session_queue as sq

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        await sq.enqueue("s1", {"name": "one"})
        await sq.enqueue("s2", {"name": "two"})
        head = await sq.next_entry()
        assert head is not None and head.session_id == "s1"
        nxt = await sq.next_entry(exclude={"s1"})
        assert nxt is not None and nxt.session_id == "s2"
        assert await sq.next_entry(exclude={"s1", "s2"}) is None
        # Default (no exclusion) is unchanged.
        assert (await sq.next_entry()).session_id == "s1"

    asyncio.run(run())
