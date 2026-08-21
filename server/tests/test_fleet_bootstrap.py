"""A single-host deployment must become a one-row fleet automatically.

Spec rollback requirement: "#4 degrades to single-host when the registry has one
entry." Nobody should have to hand-populate a table to keep working.
"""
from __future__ import annotations

import asyncio


def test_existing_endpoint_is_seeded_as_a_host(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
        name = await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return name, rows

    name, rows = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0].endpoint == "unix:///run/ccswitch/sessiond.sock"
    assert rows[0].name == name
    assert rows[0].enabled is True


def test_seeding_is_idempotent(tmp_path):
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot2.db'}")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "re-seeding must not duplicate the host"


def test_seeding_does_not_disturb_an_existing_registry(tmp_path):
    """Once an operator manages the fleet, startup must not inject rows."""
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot3.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "an operator-managed fleet must not gain a row"
