"""Database initialization and dependency helpers (T026/T027).

Runtime path (SQLite dev): init_db() calls create_all via run_sync.
Alembic is the migration path for Postgres production (see server/db/migrations/).
"""
from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, inspect as sa_inspect, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import get_settings
from server.db.models import Base

log = logging.getLogger(__name__)


class SchemaDriftError(RuntimeError):
    """Raised at startup when the live DB is missing model columns that reconcile
    could not add — a drifted server must refuse to start, not serve empty."""


# Module-level singletons — replaced by init_db() at app startup.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine(db_url: str, *, poolclass=None):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Pass timeout at the sqlite3.connect() level so it takes effect
        # regardless of greenlet context.  The aiosqlite dialect forwards
        # connect_args to sqlite3.connect(**kwargs) where `timeout` is the
        # busy-wait duration in seconds before raising OperationalError.
        # The PRAGMA busy_timeout event below is belt-and-suspenders; the
        # connect_args path is the one that actually lands on every connection.
        connect_args["timeout"] = 30
    engine_kwargs = {"connect_args": connect_args, "echo": False}
    # `poolclass` lets a caller pin the pool strategy (the test harness pins
    # StaticPool so a file-backed stand-in for an in-memory DB keeps the same
    # single-connection semantics). Default None = SQLAlchemy's own choice.
    if poolclass is not None:
        engine_kwargs["poolclass"] = poolclass
    engine = create_async_engine(db_url, **engine_kwargs)
    if db_url.startswith("sqlite"):
        # WAL = concurrent readers + a single serialized writer, so the daemon
        # (sessions) and edge (audit/settings) can share one DB file across
        # processes; busy_timeout makes a contended writer wait instead of
        # raising "database is locked". Set per-connection via the sync driver.
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver callback
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return engine


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


def _normalize_pending_rearm_null(sync_conn) -> None:
    """Rewrite the JSON-`null` sentinel in ``sessions.pending_rearm`` to SQL NULL.

    Before the column used ``JSON(none_as_null=True)``, clearing it via ``None`` stored
    the JSON literal text ``'null'`` rather than SQL NULL. Such rows are harmless (the
    rearm query's Python ``if r.pending_rearm`` filter skips them) but make
    ``pending_rearm IS NOT NULL`` over-select them. One-time, idempotent backfill:
    matches nothing once the rows are clean.
    """
    if "sessions" in set(sa_inspect(sync_conn).get_table_names()):
        sync_conn.execute(sa_text(
            "UPDATE sessions SET pending_rearm = NULL WHERE pending_rearm = 'null'"
        ))


def _assert_no_schema_drift(sync_conn) -> None:
    """After reconcile, verify every model column exists in its table.

    A residual gap (a column reconcile could not synthesize) means queries
    selecting it will raise at runtime — so fail fast and loud here instead.
    """
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it — fully formed
        present = {col["name"] for col in inspector.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in present]
        if missing:
            drift[table.name] = missing
    if drift:
        raise SchemaDriftError(
            f"DB is missing model columns reconcile could not add: {drift}. "
            "Add a synthesizable default or a real migration."
        )


async def init_db(db_url: str | None = None) -> None:
    """Create tables (SQLite dev path) and initialise the session factory."""
    global _engine, _session_factory
    settings = get_settings()
    url = db_url or settings.db_url
    # Dispose any prior engine before replacing it, so its pooled aiosqlite
    # connection (and worker thread) don't leak. This is the fallback for callers
    # that re-init without a clean shutdown (chiefly tests, which build a fresh
    # in-memory DB per app); the primary teardown is dispose_db() on app shutdown.
    # Best-effort: an engine bound to an already-closed event loop can't clean up
    # gracefully, and that path is exactly what dispose_db() exists to avoid.
    if _engine is not None:
        with contextlib.suppress(Exception):
            await _engine.dispose()
    _engine = _make_engine(url)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if url.startswith("sqlite"):
            await conn.run_sync(_reconcile_sqlite_columns)
            await conn.run_sync(_assert_no_schema_drift)
            await conn.run_sync(_normalize_pending_rearm_null)


async def dispose_db() -> None:
    """Dispose the engine and clear the session factory (app shutdown / test teardown).

    Must run while the owning event loop is still alive so the pooled aiosqlite
    connection's worker thread is joined cleanly. Skipping this — as the app did
    before — leaves one connection per app bound to a loop that then closes; its
    later cleanup callback fires `RuntimeError: Event loop is closed`, and across a
    full test run those leaked connections/threads pile up (100+ per run).
    """
    global _engine, _session_factory
    if _engine is not None:
        with contextlib.suppress(Exception):
            await _engine.dispose()
    _engine = None
    _session_factory = None


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
