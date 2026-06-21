"""End-to-end: reconcile must bridge an OLD, populated sessions table up to the
current model — the exact scenario fresh-DB tests miss and the 2026-06-21
incident hit. The baseline is intentionally ancient (worst case); it never needs
updating as the model grows.
"""
from __future__ import annotations

import asyncio
import sqlite3

from sqlalchemy import inspect as sa_inspect, text as sa_text

from server.db import init_db, db_session
from server.db.models import Session

# The oldest sessions shape we must still upgrade from (pre-repo/title/etc.).
_LEGACY_SESSIONS_DDL = (
    "CREATE TABLE sessions ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " account_name TEXT NOT NULL,"
    " status TEXT NOT NULL,"
    " cwd TEXT NOT NULL)"
)


def test_reconcile_upgrades_legacy_db_and_preserves_row(tmp_path):
    db_file = tmp_path / "legacy.db"
    con = sqlite3.connect(db_file)
    con.execute(_LEGACY_SESSIONS_DDL)
    con.execute(
        "INSERT INTO sessions (id, name, account_name, status, cwd)"
        " VALUES ('legacy1', 'My Session', 'acct1', 'idle', '/home/x')"
    )
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _check():
        # (a) every current Session model column now exists in the DB
        async with db_session() as db:
            conn = await db.connection()
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in sa_inspect(c).get_columns("sessions")})
        model_cols = {c.name for c in Session.__table__.columns}
        missing = model_cols - cols
        assert not missing, f"reconcile left columns missing: {missing}"

        # (b) the pre-existing row still reads back (round-trip against real data)
        async with db_session() as db:
            row = (await db.execute(
                sa_text("SELECT id, name, account_name, status, cwd FROM sessions WHERE id='legacy1'"))).first()
        assert row == ("legacy1", "My Session", "acct1", "idle", "/home/x")

        # (c) the ORM can load the row — the EXACT incident query shape — and the
        # reconciled JSON/datetime columns deserialize correctly.
        from sqlalchemy import select as _select
        async with db_session() as db:
            obj = (await db.execute(_select(Session).limit(1))).scalars().first()
        assert obj is not None
        assert obj.tool_allowlist == []       # JSON '[]' literal → []
        assert obj.created_at is not None      # epoch sentinel datetime

    asyncio.run(_check())
