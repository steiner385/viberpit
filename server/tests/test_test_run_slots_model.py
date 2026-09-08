"""test_run_slots table roundtrip (#822 verification-phase test-run slot cap)."""
import asyncio


def test_test_run_slot_roundtrip(tmp_path):
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        from sqlalchemy import select
        from server.db.models import TestRunSlot
        async with db_session() as db:
            db.add(TestRunSlot(session_id="s1", host="rocky", status="holding"))
        async with db_session() as db:
            row = (await db.execute(select(TestRunSlot))).scalar_one()
            assert row.session_id == "s1"
            assert row.host == "rocky"
            assert row.status == "holding"
            assert row.requested_at is not None

    asyncio.run(run())


def test_test_run_slot_status_default_is_waiting(tmp_path):
    """The Python-side default matters for the SQLite reconcile ALTER-ADD path
    (an existing row backfilled by reconcile gets this value, not NULL)."""
    from server.db import db_session, init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't2.db'}")
        from sqlalchemy import select
        from server.db.models import TestRunSlot
        async with db_session() as db:
            db.add(TestRunSlot(session_id="s2", host="rocky"))
        async with db_session() as db:
            row = (await db.execute(select(TestRunSlot))).scalar_one()
            assert row.status == "waiting"

    asyncio.run(run())
