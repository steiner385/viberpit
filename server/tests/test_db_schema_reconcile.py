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

import pytest
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


def test_init_db_adds_not_null_datetime_column_with_epoch_sentinel(tmp_path):
    """An old sessions table missing the NOT NULL `created_at` (default=datetime.utcnow)
    gains it via a fixed epoch sentinel ('1970-01-01 00:00:00') rather than being skipped."""
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
    assert asyncio.run(_read()) == "1970-01-01 00:00:00"   # backfilled with the epoch sentinel


def test_init_db_normalizes_pending_rearm_json_null_to_sql_null(tmp_path):
    """A row whose pending_rearm holds the JSON literal text 'null' (from clearing it
    via None before the column used none_as_null=True) is normalized to SQL NULL, so
    the rearm query's `pending_rearm IS NOT NULL` no longer over-selects it."""
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, name TEXT, account_name TEXT, status TEXT, cwd TEXT,"
        " pending_rearm TEXT)"
    )
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd, pending_rearm)"
                " VALUES ('jn','n','acct','idle','/tmp','null')")          # JSON-null sentinel
    con.execute("INSERT INTO sessions (id, name, account_name, status, cwd, pending_rearm)"
                " VALUES ('real','n','acct','idle','/tmp','[{\"x\": 1}]')")  # real snapshot kept
    con.commit(); con.close()

    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))

    async def _read():
        async with db_session() as db:
            over = (await db.execute(sa_text(
                "SELECT count(*) FROM sessions WHERE pending_rearm IS NOT NULL"))).scalar_one()
            jn = (await db.execute(sa_text(
                "SELECT pending_rearm FROM sessions WHERE id='jn'"))).scalar_one()
            return over, jn
    over, jn = asyncio.run(_read())
    assert jn is None              # 'null' text rewritten to SQL NULL
    assert over == 1               # only the real snapshot remains IS NOT NULL


def test_ddl_default_literal_handles_dict_default():
    """A NOT NULL column with default=dict (callable producing {}) synthesizes '{}'."""
    from sqlalchemy import Column, JSON
    from server.db import _ddl_default_literal
    col = Column("x", JSON, nullable=False, default=dict)
    assert _ddl_default_literal(col) == "'{}'"


def test_init_db_raises_schema_drift_when_reconcile_cannot_fix(tmp_path, monkeypatch):
    """If reconcile fails to add a model column, init_db must fail LOUD (raise
    SchemaDriftError) rather than letting the server start and serve empty."""
    from server.db import SchemaDriftError
    db_file = tmp_path / "drift.db"
    con = sqlite3.connect(db_file)
    # Old table missing several NOT NULL model columns.
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, cwd TEXT)")
    con.commit(); con.close()

    # Disable reconcile so the drift is NOT healed → the self-check must catch it.
    monkeypatch.setattr("server.db._reconcile_columns", lambda sync_conn: None)

    with pytest.raises(SchemaDriftError):
        asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{db_file}"))
