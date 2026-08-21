"""Host registry model + Session.host column."""
from __future__ import annotations

import asyncio


def test_host_row_roundtrips_with_defaults(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fleet.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky.tail0dd570.ts.net:9443"))
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()

    row = asyncio.run(run())
    assert row.enabled is True, "hosts are enabled by default"
    assert row.weight == 100, "default weight is 100"
    assert row.max_sessions is None
    assert row.healthy is None, "health is unknown until first probe"


def test_session_records_its_host(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Session as DbSession

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sess.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="S", account_name="a", status="idle",
                             cwd="/tmp/s1", host="rocky"))
        async with db_session() as db:
            return (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()

    assert asyncio.run(run()).host == "rocky"
