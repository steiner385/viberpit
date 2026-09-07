#!/usr/bin/env python3
"""One-shot SQLite -> Postgres copy for ccswitch. Idempotent and resumable.

*** CUTOVER PRECONDITION -- READ BEFORE RUNNING THE AUTHORITATIVE --apply ***
`ON CONFLICT DO NOTHING` means this script only ever INSERTs; it never UPDATEs a
row that already exists in Postgres. If ccswitch writers are still running, a row
copied on an early --apply (e.g. `sessions.updated_at`, `ci_finding.last_seen_at`,
`automation.last_fired_at`, `app_sessions.revoked_at`) will silently keep its OLD
value forever, even though the live app has since mutated it in SQLite -- the
re-run's INSERT for that row's PK is a no-op conflict, not a refresh. The
AUTHORITATIVE run that Postgres cutover depends on MUST happen after ccswitch
writers are stopped. Any --apply run before that point is only a throughput
pre-warm (see below) -- its rows must be treated as potentially stale, not as
a completed migration.

Retention-scoped by design (spec sub-project 1b): ci_test_result carries ~11.1M rows,
almost all of which the running _rollup_passed job folds into ci_test_daily within a
week. Copying them would be slow and pointless, so we copy only what retention keeps:
every failed/error row, plus passed/skipped rows inside the raw window, plus any row
with a NULL outcome/created_ts (mirrors _rollup_passed's own delete predicate, which
never deletes a NULL -- see _retention_where).

The SQLite file is never modified. It remains the rollback and the archive.

FK enforcement: SQLite never enforced foreign keys on this database, but Postgres
does. A live audit found automation_event rows whose session_id points at a
session that no longer exists -- the column is nullable and declared
ondelete="SET NULL", so this is exactly the state that column was designed to hold;
SQLite simply never enforced it. Before copying any table, this script walks every
FK column on it, looks up the parent's key set (from SQLite, respecting the parent's
own retention filter if it has one), and:
  - nulls out orphaned values in *nullable* FK columns, reporting the count -- never
    dropping the row, since the row itself is real data;
  - refuses to guess on a *non-nullable* FK column with orphans: dropping such a row
    is a data-loss decision this script does not make. It raises SystemExit(BLOCKED).

Row-value normalization: this script reads rows via the raw sqlite3 driver (not
through SQLAlchemy's type decoders), so DateTime and JSON columns arrive as plain
SQLite TEXT rather than Python datetime/dict objects. Passed through as-is, asyncpg
rejects DateTime strings outright and silently double-encodes JSON strings (it
re-serializes an already-serialized string). Both are normalized generically, per
column, from each table's declared SQLAlchemy type before insert.

Identity sequences: copying explicit PK values does NOT advance a Postgres
SERIAL/IDENTITY sequence -- the sequence stays at its initial state while the table
fills up around it, so the app's first INSERT without an explicit id (the normal
case) raises `duplicate key value violates unique constraint`. After the copy loop,
--apply advances every single-column integer-PK table's sequence to MAX(pk) via
`setval`; --verify asserts `last_value >= MAX(pk)` for the same set of tables.
Tables with a non-integer or composite PK have no such sequence and are skipped.

Dry run intentionally still runs `create_all` against the target (additive-only DDL:
CREATE TABLE/INDEX IF NOT EXISTS-equivalent semantics -- it never alters or drops
anything). This is deliberate, not an oversight: --verify needs the target schema to
exist even before the first --apply, so a plain dry run against a brand-new
database reports real `pg=0` counts instead of erroring on missing tables.

  python scripts/viberpit-sqlite-to-pg.py                 # dry run: report counts
  python scripts/viberpit-sqlite-to-pg.py --apply         # copy (creates schema)
  python scripts/viberpit-sqlite-to-pg.py --verify        # compare counts + sequences
"""
import argparse
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from sqlalchemy import DateTime, func, select as sa_select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.types import JSON

from server.db.models import Base

SQLITE = os.environ.get("CCSWITCH_SQLITE_PATH",
                        "/home/tony/.claude-accounts/ccswitch.db")
BATCH = 5_000


def _retention_where(table: str, raw_days: float) -> str:
    """Only ci_test_result is scoped; every other table copies in full."""
    if table != "ci_test_result":
        return ""
    horizon = time.time() - raw_days * 86400.0
    # Mirrors _rollup_passed's delete predicate exactly: that job deletes rows
    # WHERE outcome NOT IN ('failed','error') AND created_ts < horizon, and SQL's
    # NULL semantics mean a NULL outcome/created_ts never satisfies that AND, so
    # _rollup_passed never deletes a NULL row. Our keep-predicate must therefore
    # also keep NULLs, or this script would drop rows the live retention job
    # would not have dropped. (Live NULL count is 0 today -- this is a latent
    # correctness fix, not something observed to have dropped a row yet.)
    return (f" WHERE outcome IS NULL OR created_ts IS NULL "
            f"OR outcome IN ('failed','error') OR created_ts >= {horizon}")


def _combine_where(base_where: str, extra_sql: str) -> str:
    """Append `extra_sql` to a possibly-empty `base_where` (as returned by
    _retention_where) without breaking OR precedence inside `base_where`."""
    if not base_where:
        return f" WHERE {extra_sql}"
    condition = base_where.strip()[len("WHERE "):]
    return f" WHERE ({condition}) AND {extra_sql}"


def _fk_columns(table):
    """Yield (column, parent_table_name, parent_column_name) for every FK column
    declared on `table`, excluding self-references (none exist in this schema, and
    a self-reference would need topological handling this script does not do)."""
    for col in table.columns:
        for fk in col.foreign_keys:
            if fk.column.table.name == table.name:
                continue
            yield col, fk.column.table.name, fk.column.name


def _row_coercers(table):
    """Per-column (name, fn) pairs that convert a raw sqlite3 TEXT value into
    what asyncpg expects, for the column types where raw TEXT is not enough:
    DateTime (needs a real datetime.datetime; re-attaches UTC tzinfo for
    timezone=True columns, since SQLite's DATETIME storage strips tzinfo on
    write but every writer in this codebase uses UTC) and JSON (needs the
    Python object, not the already-serialized string, or SQLAlchemy's JSON
    bind processor double-encodes it)."""
    coercers = []
    for col in table.columns:
        if isinstance(col.type, DateTime):
            tz_aware = bool(getattr(col.type, "timezone", False))

            def _conv_dt(v, tz_aware=tz_aware):
                if v is None:
                    return None
                dt = datetime.fromisoformat(v)
                if tz_aware and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                elif not tz_aware and dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                return dt

            coercers.append((col.name, _conv_dt))
        elif isinstance(col.type, JSON):
            def _conv_json(v):
                if v is None or isinstance(v, (dict, list)):
                    return v
                return json.loads(v)

            coercers.append((col.name, _conv_json))
    return coercers


def _single_pk_name(table):
    """Return the column name of `table`'s PK iff it is a single column
    (sequences only ever back a single-column PK); None for composite PKs."""
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return None
    return pk_cols[0].name


async def _advance_sequences(engine, have: set) -> None:
    """After a copy, advance every single-column-PK table's identity sequence to
    MAX(pk). Copying explicit PK values never moves the sequence itself, so
    without this the app's first sequence-generated INSERT after cutover raises
    `duplicate key value violates unique constraint` -- and keeps raising, since
    the sequence never catches up on its own. pg_get_serial_sequence returns NULL
    for PKs with no backing sequence (e.g. String/UUID PKs); those are skipped,
    not errored."""
    print("\nAdvancing identity sequences...")
    for table in Base.metadata.sorted_tables:
        if table.name not in have:
            continue
        pk = _single_pk_name(table)
        if pk is None:
            continue
        async with engine.begin() as c:
            seq = (await c.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": pk})).scalar_one()
            if seq is None:
                continue
            newval = (await c.execute(text(
                f'SELECT setval(:seq, COALESCE((SELECT MAX("{pk}") FROM "{table.name}"), 1))'),
                {"seq": seq})).scalar_one()
            print(f"  {table.name:<28} sequence {seq} -> {newval}")


async def _verify_sequences(engine, have: set) -> None:
    """--verify companion to _advance_sequences: assert last_value >= MAX(pk) for
    every sequenced table, so a 'verified' cutover can't hide an unadvanced
    sequence behind all-OK row counts."""
    for table in Base.metadata.sorted_tables:
        if table.name not in have:
            continue
        pk = _single_pk_name(table)
        if pk is None:
            continue
        async with engine.connect() as c:
            seq = (await c.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": pk})).scalar_one()
            if seq is None:
                continue
            last_value = (await c.execute(text(f"SELECT last_value FROM {seq}"))).scalar_one()
            max_pk = (await c.execute(sa_select(func.max(table.c[pk])))).scalar_one()
            ok = max_pk is None or last_value >= max_pk
            flag = "OK " if ok else "DIFF"
            print(f"  {flag} {table.name + '.' + pk + ' seq':<28} last_value={last_value:<10} max_pk={max_pk}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-url", default=os.environ.get("CCSWITCH_DB_URL"))
    ap.add_argument("--raw-days", type=float, default=7.0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if not args.pg_url:
        raise SystemExit("need --pg-url or CCSWITCH_DB_URL")
    if args.apply and args.verify:
        raise SystemExit("--apply and --verify are mutually exclusive -- run one at a time "
                          "(apply, THEN verify, as two separate invocations)")

    src = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)  # READ-ONLY
    src.row_factory = sqlite3.Row
    engine = create_async_engine(args.pg_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    parent_keys_cache: dict[tuple[str, str], set] = {}

    def parent_keys(parent_table: str, parent_col: str) -> set:
        cache_key = (parent_table, parent_col)
        if cache_key not in parent_keys_cache:
            if parent_table not in have:
                parent_keys_cache[cache_key] = set()
            else:
                pwhere = _retention_where(parent_table, args.raw_days)
                rows = src.execute(
                    f'SELECT "{parent_col}" FROM "{parent_table}"{pwhere}').fetchall()
                parent_keys_cache[cache_key] = {r[0] for r in rows}
        return parent_keys_cache[cache_key]

    total_nulled = 0

    for table in Base.metadata.sorted_tables:
        if table.name not in have:
            print(f"  {table.name:<28} absent in SQLite — skipped")
            continue
        where = _retention_where(table.name, args.raw_days)
        n = src.execute(f"SELECT COUNT(*) FROM \"{table.name}\"{where}").fetchone()[0]

        if args.verify or not args.apply:
            async with engine.connect() as c:
                dst = (await c.execute(sa_select(func.count()).select_from(table))).scalar_one()
            flag = "OK " if dst == n else "DIFF"
            print(f"  {flag} {table.name:<28} sqlite={n:<10} pg={dst}")
            continue

        # FK guard, general over every declared FK column on this table (see module
        # docstring). Checked BEFORE touching Postgres so a BLOCKED table aborts
        # cleanly with nothing written for it.
        fk_cols = list(_fk_columns(table))
        nullable_guards = []
        for col, parent_table, parent_col in fk_cols:
            valid = parent_keys(parent_table, parent_col)
            child_where = _combine_where(where, f'"{col.name}" IS NOT NULL')
            rows = src.execute(
                f'SELECT "{col.name}" FROM "{table.name}"{child_where}').fetchall()
            orphan_n = sum(1 for r in rows if r[0] not in valid)
            if orphan_n == 0:
                continue
            if not col.nullable:
                raise SystemExit(
                    f"BLOCKED: {table.name}.{col.name} is NOT NULL and references "
                    f"{parent_table}.{parent_col}, but {orphan_n} row(s) reference a "
                    f"missing parent. Dropping rows is a data-loss decision this "
                    f"script will not make automatically -- resolve manually.")
            print(f"  {table.name}.{col.name}: {orphan_n} orphaned reference(s) to "
                  f"{parent_table}.{parent_col} will be set NULL (column is nullable, "
                  f"ondelete=SET NULL semantics)")
            nullable_guards.append((col.name, valid))

        cols = [c.name for c in table.columns]
        coercers = _row_coercers(table)
        cur = src.execute(
            f"SELECT {', '.join(chr(34)+c+chr(34) for c in cols)} "
            f"FROM \"{table.name}\"{where}")
        copied = 0
        table_nulled = 0
        skipped_in_chunk = 0
        while True:
            chunk = cur.fetchmany(BATCH)
            if not chunk:
                break
            payload = [dict(zip(cols, row)) for row in chunk]
            for col_name, fn in coercers:
                for row in payload:
                    row[col_name] = fn(row[col_name])
            for col_name, valid in nullable_guards:
                for row in payload:
                    v = row.get(col_name)
                    if v is not None and v not in valid:
                        row[col_name] = None
                        table_nulled += 1
            async with engine.begin() as c:
                # ON CONFLICT DO NOTHING makes a re-run after a partial failure safe
                # (see the cutover-precondition warning at the top of this file for
                # what "safe" does NOT cover: it does not refresh rows that already
                # existed in Postgres and have since changed in SQLite).
                result = await c.execute(pg_insert(table).on_conflict_do_nothing(), payload)
            # rowcount is the actual INSERT count, not the attempted count -- an
            # ON CONFLICT DO NOTHING skip must show up here, or "copied N/N" would
            # lie about a row that was silently deduped rather than inserted.
            # asyncpg's executemany path can report rowcount as -1 (unsupported);
            # fall back to the attempted count only in that case, so a genuine
            # skip is never masked by the fallback.
            inserted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 \
                else len(chunk)
            copied += inserted
            skipped_in_chunk += len(chunk) - inserted
            print(f"    {table.name}: {copied}/{n}", end="\r", flush=True)
        skip_note = f" (skipped {skipped_in_chunk} existing via ON CONFLICT)" if skipped_in_chunk else ""
        print(f"  {table.name:<28} copied {copied}/{n}{skip_note}        ")
        if table_nulled:
            print(f"    -> nulled {table_nulled} dangling FK value(s) in {table.name}")
            total_nulled += table_nulled

    if args.apply:
        await _advance_sequences(engine, have)
    if args.verify:
        print()
        await _verify_sequences(engine, have)

    await engine.dispose()
    src.close()
    if args.apply:
        print(f"\nTOTAL dangling FK values nulled across all tables: {total_nulled}")
    if not args.apply and not args.verify:
        print("\ndry run — re-run with --apply")


if __name__ == "__main__":
    # Guarded so the module can be imported (e.g. by
    # server/tests/test_pg_migration_helpers.py, which unit-tests
    # _combine_where/_row_coercers directly) without side-effect-running the
    # whole migration against argv.
    asyncio.run(main())
