"""init_db reconciles missing nullable columns on existing SQLite dev DBs.

`Base.metadata.create_all` only creates *absent tables* — it never adds a new
column to a table that already exists.  So a dev SQLite DB created before a
column was added to a model would be missing that column, and any query touching
it would raise "no such column".  init_db() closes this gap for the SQLite dev
path by ALTER TABLE ADD COLUMN-ing any nullable mapped column that's missing.
"""
from __future__ import annotations

import asyncio
import sqlite3

from sqlalchemy import text as sa_text

from server.db import db_session, init_db


def test_init_db_adds_missing_nullable_repo_column(tmp_path):
    """A pre-existing sessions table without `repo` gains it after init_db."""
    db_file = tmp_path / "old.db"

    # Simulate an OLD dev DB: a sessions table created before `repo` existed.
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT"
        ")"
    )
    con.commit()
    con.close()

    db_url = f"sqlite+aiosqlite:///{db_file}"
    asyncio.run(init_db(db_url=db_url))

    async def _roundtrip_repo() -> str:
        # If `repo` was added, this INSERT+SELECT succeeds; otherwise the INSERT
        # raises OperationalError: table sessions has no column named repo.
        async with db_session() as db:
            await db.execute(sa_text(
                "INSERT INTO sessions (id, name, account_name, status, cwd, repo)"
                " VALUES ('s1', 'n', 'acct', 'idle', '/tmp', 'acme/widget')"
            ))
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT repo FROM sessions WHERE id = 's1'")
            )).scalar_one()

    assert asyncio.run(_roundtrip_repo()) == "acme/widget"
