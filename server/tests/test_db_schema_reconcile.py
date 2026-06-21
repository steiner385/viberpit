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


def test_init_db_adds_missing_not_null_column_with_default(tmp_path):
    """A pre-existing, POPULATED sessions table missing the NOT NULL
    `name_is_auto` column gains it (backfilled to the model default) after
    init_db — the schema-drift that broke the auto-deployed server when master
    added `name_is_auto` while the live SQLite DB predated it.
    """
    db_file = tmp_path / "old.db"

    # OLD dev DB created before `name_is_auto` existed, with a row already in it.
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT"
        ")"
    )
    con.execute(
        "INSERT INTO sessions (id, name, account_name, status, cwd)"
        " VALUES ('old1', 'n', 'acct', 'idle', '/tmp')"
    )
    con.commit()
    con.close()

    db_url = f"sqlite+aiosqlite:///{db_file}"
    asyncio.run(init_db(db_url=db_url))

    async def _read_name_is_auto() -> int:
        # Pre-existing row must be backfilled with the model default (True → 1).
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT name_is_auto FROM sessions WHERE id = 'old1'")
            )).scalar_one()

    assert asyncio.run(_read_name_is_auto()) == 1


def test_init_db_adds_not_null_json_column_with_list_default(tmp_path):
    """An old populated sessions table missing the NOT NULL `tool_allowlist`
    (JSON, default=list) column gains it, backfilled to '[]'."""
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT)"
    )
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd)"
                " VALUES ('o1','n','acct','idle','/tmp')")
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _read():
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT tool_allowlist FROM sessions WHERE id='o1'"))).scalar_one()
    # JSON column stored as TEXT; the backfilled default literal is '[]'.
    assert asyncio.run(_read()) == "[]"


def test_init_db_adds_not_null_datetime_column_with_current_timestamp(tmp_path):
    """An old sessions table missing the NOT NULL `created_at` (default=datetime.utcnow)
    gains it via CURRENT_TIMESTAMP rather than being skipped."""
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT)"
    )
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd)"
                " VALUES ('o2','n','acct','idle','/tmp')")
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _read():
        async with db_session() as db:
            return (await db.execute(
                sa_text("SELECT created_at FROM sessions WHERE id='o2'"))).scalar_one()
    assert asyncio.run(_read()) is not None   # backfilled with a real timestamp
