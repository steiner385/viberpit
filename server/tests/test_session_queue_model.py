"""session_queue table + status SoT + max_active_sessions setting."""
import asyncio


def test_status_sets_and_default_cap():
    """`queued` is not an inactive status — it is a runtime-capacity exemption only
    (CAPACITY_EXEMPT_STATUSES). The old NON_ACTIVE_SESSION_STATUSES that conflated
    the two had no production consumer and was deleted."""
    from server.db.models import (CAPACITY_EXEMPT_STATUSES,
                                  DEFAULT_MAX_ACTIVE_SESSIONS,
                                  INACTIVE_SESSION_STATUSES)
    assert INACTIVE_SESSION_STATUSES == frozenset({"ended", "archived"})
    assert CAPACITY_EXEMPT_STATUSES == INACTIVE_SESSION_STATUSES | {"queued", "complete"}
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
