"""Database initialization and dependency helpers (T026/T027).

Runtime path (SQLite dev): init_db() calls create_all via run_sync.
Alembic is the migration path for Postgres production (see server/db/migrations/).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, cast

from sqlalchemy import (
    Table, event, insert as sa_insert, inspect as sa_inspect, literal as sa_literal,
    select as sa_select, text as sa_text, update as sa_update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.aio import run_to_completion
from server.config import get_settings
from server.db.models import Automation, Base, DataBackfill, WorkItem
from server.sessions.engine import AgentEngine

log = logging.getLogger(__name__)


class SchemaDriftError(RuntimeError):
    """Raised at startup when the live DB is missing model columns that reconcile
    could not add — a drifted server must refuse to start, not serve empty."""


# Module-level singletons — replaced by init_db() at app startup.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Budget for dispose_db()'s GRACEFUL engine.dispose() before it drops the pool
# without awaiting the wedged connection (see dispose_db). Kept well under
# app.py's _SHUTDOWN_DB_TIMEOUT_S (10s) bounded_wait budget so dispose_db falls
# back and returns on its own rather than being force-abandoned (issue #881).
_DISPOSE_GRACE_TIMEOUT_S = 5.0


def _make_engine(db_url: str, *, poolclass=None):
    connect_args: dict[str, object] = {}
    if db_url.startswith("sqlite"):
        # The dev default lives in ~/.claude-accounts, an sshfs mount on every
        # session runner: refuse a SQLite file there before opening it (#2016).
        from server.localstate import assert_local_sqlite, sqlite_file_from_url
        if (sqlite_file := sqlite_file_from_url(db_url)) is not None:
            assert_local_sqlite(sqlite_file)
        # Before the first aiosqlite connection exists: upstream's worker thread
        # dies with an unhandled "Event loop is closed" when it delivers a result
        # to a loop that closed under an abandoned borrower (bounded_wait does
        # exactly that on a teardown overrun), taking the SQLite handle with it.
        # See server/db/aiosqlite_guard.py (issue #1134).
        from server.db.aiosqlite_guard import install_loop_safe_worker
        install_loop_safe_worker()
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
    # NullPool for its file-backed stand-in for an in-memory DB, so there is no
    # single shared wedge-able connection to hang teardown — issue #890).
    # Default None = SQLAlchemy's own choice.
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


def _reconcile_columns(sync_conn) -> None:
    """Add any mapped column missing from an existing table, on any dialect.

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


def _reconcile_fk_on_delete(sync_conn) -> None:
    """Rewrite any live foreign key whose ON DELETE rule differs from the model's.

    Postgres only: SQLite cannot ALTER a constraint (and does not enforce FKs by
    default, so a stale rule there is inert). The live database was built by
    ``create_all`` and nothing runs Alembic on rollout, so a model that gains
    ``ondelete=`` on an existing FK (``dispatch_outcomes.session_id`` → SET NULL,
    #2074's sibling on the approval path) would otherwise keep the bare
    constraint forever and every session delete would keep raising
    ForeignKeyViolationError. Same single-source-of-truth shape as
    ``_reconcile_columns``: the models say what the constraint should be,
    matched by constrained columns, and a mismatch is DROPped and re-ADDed under
    its existing name. Idempotent — matches nothing once the rules agree.
    """
    if sync_conn.dialect.name != "postgresql":
        return
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it with the model's rule
        wanted = {tuple(c.name for c in fk.columns): fk
                  for fk in table.foreign_key_constraints}
        for live in inspector.get_foreign_keys(table.name):
            fk = wanted.get(tuple(live["constrained_columns"]))
            if fk is None or not live.get("name"):
                continue
            desired = (fk.ondelete or "NO ACTION").upper()
            actual = ((live.get("options") or {}).get("ondelete") or "NO ACTION").upper()
            if desired == actual:
                continue
            name = live["name"]
            cols = ", ".join(f'"{c.name}"' for c in fk.columns)
            ref_cols = ", ".join(f'"{e.column.name}"' for e in fk.elements)
            rule = "" if desired == "NO ACTION" else f" ON DELETE {desired}"
            log.info("reconcile: rewriting %s.%s ON DELETE %s -> %s",
                     table.name, name, actual, desired)
            sync_conn.execute(sa_text(
                f'ALTER TABLE "{table.name}" DROP CONSTRAINT "{name}"'))
            sync_conn.execute(sa_text(
                f'ALTER TABLE "{table.name}" ADD CONSTRAINT "{name}" '
                f'FOREIGN KEY ({cols}) REFERENCES "{fk.referred_table.name}" ({ref_cols})'
                f'{rule}'))


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


def _migrate_weighted_selection_strategy(sync_conn) -> None:
    """Rewrite a stored Backlog Runner ``selection_strategy: weighted`` to ``priority``.

    ``weighted`` was removed from the enum (spec 2026-09-20 D6, #2103); it already
    ordered exactly like ``priority``. A stored row still carrying it would fail enum
    validation and every reader would silently fall back to schema defaults.
    Idempotent: matches nothing once the row is clean.
    """
    if "automation" not in set(sa_inspect(sync_conn).get_table_names()):
        return
    table = cast(Table, Automation.__table__)
    row = sync_conn.execute(
        sa_select(table.c.config).where(table.c.id == "backlog-runner")).first()
    config = row[0] if row else None
    if isinstance(config, dict) and config.get("selection_strategy") == "weighted":
        sync_conn.execute(sa_update(table).where(table.c.id == "backlog-runner")
                          .values(config={**config, "selection_strategy": "priority"}))


def _backfill_session_engine(sync_conn) -> None:
    """Idempotently migrate legacy session identity into the engine-neutral columns.

    Production startup uses reconciliation rather than Alembic. The legacy physical
    column may therefore remain on an existing database even though it is deliberately
    absent from the ORM. It is read only here, during migration, and never by runtime
    session code.
    """
    inspector = sa_inspect(sync_conn)
    if "sessions" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "engine" in columns:
        sync_conn.execute(sa_text(
            "UPDATE sessions SET engine = :engine WHERE engine IS NULL"
        ), {"engine": AgentEngine.CLAUDE_CODE.value})
    if {"engine_session_id", "claude_session_id"}.issubset(columns):
        sync_conn.execute(sa_text(
            "UPDATE sessions SET engine_session_id = claude_session_id "
            "WHERE engine_session_id IS NULL"
        ))


# GitHub App logins the mirror used to store bare (it now writes Bots as ``app/<slug>``).
# Both were verified unregistered as USER logins on 2026-10-09, so a bare row of either
# name written by the OLD sync code (before it read ``__typename``) can only be the App.
_APP_AUTHOR_SLUGS = ("github-actions", "viberpit")
_APP_AUTHOR_BACKFILL = "work_item_app_authors"
# Extra conservative bound on ``gh_created_at``: nothing created after the logins were
# verified unregistered is ever relabelled.
_APP_AUTHOR_BACKFILL_BOUND = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _backfill_app_authors(sync_conn) -> None:
    """ONE-SHOT: relabel bare App authors the old mirror wrote as ``app/<slug>``.

    Same rationale as ``_backfill_session_engine``: production startup reconciles
    rather than running Alembic, so migration 0081's data change only reaches the live
    database from here.

    SECURITY -- this MUST stay one-shot (the ``data_backfills`` marker, written in the
    same transaction as the UPDATE); do not "simplify" it into an every-boot update,
    and a ``gh_created_at`` bound alone is NOT enough. GitHub users can rename: someone
    who opened an issue before the bound can later rename to the unregistered login
    ``viberpit``/``github-actions``, the next sync then stores that bare login on an
    OLD row, and a repeated relabel would turn it into the trusted ``app/<slug>`` --
    the squatting bypass the ``app/`` form exists to close. Once the marker exists
    this never runs again; a bare row stays bare (untrusted, fail closed). The UPDATE
    is further bounded to rows the mirror last wrote before this run
    (``last_synced_at``) and created before 2026-10-10.
    """
    tables = set(sa_inspect(sync_conn).get_table_names())
    if not {"work_items", "data_backfills"} <= tables:
        return
    marker = cast(Table, DataBackfill.__table__)
    if sync_conn.execute(sa_select(marker.c.name).where(
            marker.c.name == _APP_AUTHOR_BACKFILL)).first() is not None:
        return
    now = datetime.now(timezone.utc)
    table = cast(Table, WorkItem.__table__)
    sync_conn.execute(
        sa_update(table)
        .where(table.c.author.in_(_APP_AUTHOR_SLUGS),
               table.c.gh_created_at < _APP_AUTHOR_BACKFILL_BOUND,
               table.c.last_synced_at < now)
        .values(author=sa_literal("app/") + table.c.author))
    sync_conn.execute(sa_insert(marker).values(name=_APP_AUTHOR_BACKFILL, applied_at=now))


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
    """Create/reconcile tables on any dialect and initialise the session factory."""
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
        # Reconcile + drift-assert on EVERY dialect. These were once SQLite-gated,
        # back when the live deployment was SQLite; the Postgres cutover silently
        # invalidated that premise and left new model columns with nothing to create
        # them — a column added to a model then deployed green and failed at runtime
        # with UndefinedColumnError on every query selecting it. Both helpers are
        # dialect-generic: reconcile compiles column types against the live dialect and
        # only ever ADDs a missing column, and the drift assert compares names only.
        await conn.run_sync(_reconcile_columns)
        await conn.run_sync(_reconcile_fk_on_delete)
        # Reconciliation only adds columns; copy legacy identity before the ORM can
        # observe the rows. This is deliberately dialect-generic because the live
        # Postgres deployment does not run Alembic revisions.
        await conn.run_sync(_backfill_session_engine)
        await conn.run_sync(_migrate_weighted_selection_strategy)
        await conn.run_sync(_backfill_app_authors)
        await conn.run_sync(_assert_no_schema_drift)
        if url.startswith("sqlite"):
            # SQLite-only: `pending_rearm = 'null'` compares a json column to text,
            # which Postgres rejects outright (its json type has no equality operator).
            # It is also a legacy fix for rows written by the pre-none_as_null SQLite code.
            await conn.run_sync(_normalize_pending_rearm_null)
    # Outside the DDL connection: this one is an ORM pass, because it reads and
    # writes JSON columns and raw SQL for those is not portable across the two
    # dialects. Safe here — the session factory is bound above.
    await _repair_former_slugs()


async def _repair_former_slugs() -> None:
    """Rebuild product slug aliases for renames that predate the column (#1880).

    Same rationale as ``_backfill_session_engine``: production startup reconciles
    rather than running Alembic, so a data repair has to live on the startup path
    to reach the live database at all. Idempotent — a no-op once every recorded
    rename is represented, and on the empty DB every test builds.
    """
    from server.pm.project_slugs import backfill_former_slugs

    async with db_session() as db:
        repaired = await backfill_former_slugs(db)
    if repaired:
        log.info("db.former_slugs_backfilled products=%s", sorted(repaired))


async def dispose_db() -> None:
    """Dispose the engine and clear the session factory (app shutdown / test teardown).

    Must run while the owning event loop is still alive so the pooled aiosqlite
    connection's worker thread is joined cleanly. Skipping this — as the app did
    before — leaves one connection per app bound to a loop that then closes; its
    later cleanup callback fires `RuntimeError: Event loop is closed`, and across a
    full test run those leaked connections/threads pile up (100+ per run).

    Graceful ``engine.dispose()`` awaits every pooled connection's close. A
    single shared, pooled connection held checked-out by an abandoned in-flight
    query (issue #846) makes that close (``aiosqlite …
    _terminate_graceful_close → _conn.close() → await future``) never return —
    wedging ``TestClient.__exit__``'s portal-thread join and tripping the 300s
    pytest-timeout (issue #881). So the graceful close is bounded: if it
    overruns, fall back to ``dispose(close=False)``, which swaps in a fresh pool
    and drops the wedged connection WITHOUT awaiting it. Leaking one connection
    at process/test teardown is harmless; hanging the whole run is not.

    The test harness (``conftest._install_durable_inmemory_db``) also pins
    ``NullPool`` (issue #890), so no single shared connection persists in the
    pool to wedge in the first place; this bounded fallback remains as
    defense-in-depth for any pooled-connection engine (e.g. production Postgres).
    """
    global _engine, _session_factory
    engine = _engine
    if engine is not None:
        try:
            await asyncio.wait_for(engine.dispose(), timeout=_DISPOSE_GRACE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning(
                "dispose_db: graceful engine.dispose() exceeded %.1fs (wedged "
                "connection) — dropping the pool without awaiting its close (#881)",
                _DISPOSE_GRACE_TIMEOUT_S,
            )
            with contextlib.suppress(Exception):
                await engine.dispose(close=False)
        except Exception:
            # e.g. an engine bound to an already-closed loop can't clean up
            # gracefully — nothing more to do; drop it and move on.
            pass
    _engine = None
    _session_factory = None


async def _commit(session: AsyncSession) -> None:
    """Commit, finishing even if the caller is cancelled mid-flush.

    ORM writes (``db.add(...)``) reach SQLite only here. A cancel landing while
    that INSERT/UPDATE is in flight invalidates the connection mid-transaction,
    and the abandoned handle keeps SQLite's one write lock until GC, so every
    other writer fails "database is locked" after the 30 s busy timeout (#2726).
    """
    await run_to_completion(session.commit(), name="db.commit")


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a DB session and commits/rolls back."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        try:
            yield session
            await _commit(session)
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
            await _commit(session)
        except Exception:
            await session.rollback()
            raise


def dialect_insert(table_or_model):
    """Return the dialect-appropriate INSERT supporting ON CONFLICT.

    SQLite and PostgreSQL both support ON CONFLICT, but through dialect-specific
    constructs (``sqlalchemy.dialects.sqlite.insert`` vs.
    ``sqlalchemy.dialects.postgresql.insert``) that are NOT interchangeable — a
    sqlite ``Insert`` executed against a Postgres engine raises
    ``AttributeError: 'OnConflictDoUpdate' object has no attribute
    'constraint_target'`` inside ``on_conflict_do_update``/``on_conflict_do_nothing``.
    Both dialect inserts otherwise expose the same call-site API
    (``on_conflict_do_update(index_elements=..., set_=...)``,
    ``on_conflict_do_nothing(index_elements=...)``, ``.excluded``), so callers can
    stay dialect-agnostic by building the statement through this helper instead of
    importing a specific dialect's ``insert``.

    Dialect is read from the live engine (``engine.dialect.name``, ``"sqlite"`` or
    ``"postgresql"``) rather than inferred from config, so it always matches the
    connection the statement will actually run on. This deliberately reads
    ``_engine`` at CALL time, not import time: the surrounding modules
    (``server/pipeline/timeline.py`` et al.) import this helper at process startup,
    before ``init_db()`` has run, so the dialect can't be resolved — or cached —
    at import time without risking a stale/wrong pick.
    """
    if _engine is None:
        raise RuntimeError(
            "Database not initialised — call init_db() before building a "
            "dialect_insert() statement"
        )
    dialect_name = _engine.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        raise RuntimeError(
            f"dialect_insert(): unsupported dialect {dialect_name!r} — only "
            "'sqlite' and 'postgresql' are supported"
        )
    return _insert(table_or_model)
