"""Database initialization and dependency helpers (T026/T027).

Runtime path (SQLite dev): init_db() calls create_all via run_sync.
Alembic is the migration path for Postgres production (see server/db/migrations/).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import get_settings
from server.db.models import Base

log = logging.getLogger(__name__)

# Module-level singletons — replaced by init_db() at app startup.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine(db_url: str):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_async_engine(db_url, connect_args=connect_args, echo=False)


def _ddl_default_literal(column) -> str | None:
    """Return a SQL literal for `column`'s default, or None if it has none.

    SQLite can ADD a NOT NULL column to a populated table only when a DEFAULT is
    supplied to backfill existing rows. We synthesize that DEFAULT from the model
    default: scalar (bool/int/float/str) directly, and the common *callable*
    defaults by evaluating them — an empty list/dict becomes a JSON literal and a
    datetime-producing default becomes a fixed epoch sentinel ('1970-01-01 00:00:00')
    — SQLite rejects CURRENT_TIMESTAMP as a non-constant DEFAULT in ALTER TABLE ADD
    COLUMN. A callable we can't map returns None so the caller skips it (and the CI
    coverage test flags it).
    """
    from datetime import datetime as _dt

    default = column.default
    if default is None:
        return None
    if getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return None
    if getattr(default, "is_callable", False):
        try:
            produced = default.arg(None)  # CallableColumnDefault takes an exec context
        except Exception:
            return None
        if isinstance(produced, _dt):
            # CURRENT_TIMESTAMP is rejected by SQLite as a non-constant DEFAULT
            # in ALTER TABLE ADD COLUMN; use a fixed sentinel instead.
            return "'1970-01-01 00:00:00'"
        if produced == []:
            return "'[]'"
        if produced == {}:
            return "'{}'"
    return None


def _reconcile_sqlite_columns(sync_conn) -> None:
    """Add any mapped column missing from an existing SQLite table.

    ``create_all`` creates absent *tables* but never adds a column to a table
    that already exists, so a dev DB created before a model gained a column would
    be missing it.  We derive the desired columns from the models (single source
    of truth) and ALTER TABLE ADD COLUMN any that are absent.

    NULLable columns are added bare. A NOT NULL column can't be added to a
    populated table without a DEFAULT to backfill existing rows — so we add it
    WITH the model's scalar default when there is one (e.g. ``name_is_auto`` /
    ``permission_mode``). A NOT NULL column whose default is non-static (a
    callable/server default) is left to a real migration and logged, since we
    can't synthesize a literal for it.
    """
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it — it already has every column
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            if column.nullable:
                sync_conn.execute(sa_text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                ))
                continue
            default_literal = _ddl_default_literal(column)
            if default_literal is None:
                log.warning(
                    "reconcile: cannot add NOT NULL column %s.%s without a static "
                    "default — needs a real migration", table.name, column.name)
                continue
            sync_conn.execute(sa_text(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f'{col_type} NOT NULL DEFAULT {default_literal}'
            ))


async def init_db(db_url: str | None = None) -> None:
    """Create tables (SQLite dev path) and initialise the session factory."""
    global _engine, _session_factory
    settings = get_settings()
    url = db_url or settings.db_url
    _engine = _make_engine(url)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if url.startswith("sqlite"):
            await conn.run_sync(_reconcile_sqlite_columns)


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a DB session and commits/rolls back."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
