"""Database initialization and dependency helpers (T026/T027).

Runtime path (SQLite dev): init_db() calls create_all via run_sync.
Alembic is the migration path for Postgres production (see server/db/migrations/).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

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


async def init_db(db_url: str | None = None) -> None:
    """Create tables (SQLite dev path) and initialise the session factory."""
    global _engine, _session_factory
    settings = get_settings()
    url = db_url or settings.db_url
    _engine = _make_engine(url)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
