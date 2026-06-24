import asyncio
from sqlalchemy import select
from server.db import init_db, db_session
from server.db.models import Session as DbSession


def test_pending_rearm_roundtrips_and_defaults_null(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path)))
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm is None            # defaults NULL
            row.pending_rearm = [{"tool_use_id": "x", "kind": "monitor",
                                  "name": "Monitor", "input": {"command": "z"},
                                  "description": "d"}]
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.pending_rearm[0]["tool_use_id"] == "x"

    asyncio.run(run())
