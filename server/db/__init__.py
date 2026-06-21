"""Database initialization and dependency helpers (T026/T027).

Runtime path (SQLite dev): init_db() calls create_all via run_sync.
Alembic is the migration path for Postgres production (see server/db/migrations/).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import get_settings
from server.db.models import Base

# Module-level singletons — replaced by init_db() at app startup.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine(db_url: str):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_async_engine(db_url, connect_args=connect_args, echo=False)


def _reconcile_sqlite_columns(sync_conn) -> None:
    """Add any nullable mapped column missing from an existing SQLite table.

    ``create_all`` creates absent *tables* but never adds a column to a table
    that already exists, so a dev DB created before a model gained a column would
    be missing it.  We derive the desired columns from the models (single source
    of truth) and ALTER TABLE ADD COLUMN any that are absent.

    Conservative on purpose: only NULLable columns are added.  A NOT NULL column
    can't be added to an already-populated table without a default, so those are
    left to a real migration (Alembic, the Postgres/prod path).
    """
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it — it already has every column
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present or not column.nullable:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(sa_text(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
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
