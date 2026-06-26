"""Migrate Plotroom's history.db (read-only) into a ccswitch-owned pipeline_history.db.

ADR-1: a SEPARATE sqlite file from the main app DB; stdlib sqlite3, not SQLAlchemy.
Only the tables the CI-ETA estimator consumes are copied. The source is WAL and
live-written — opened immutable/read-only, never written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SOURCE_DEFAULT = "/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db"

# Column order preserved verbatim from the source schema (history.ts).
_SCHEMA = {
    "check_durations": (
        "repo TEXT, check_name TEXT, event TEXT, duration_secs REAL, completed_at TEXT, "
        "conclusion TEXT, head_sha TEXT, run_attempt INTEGER, started_at TEXT, run_number INTEGER"
    ),
    "runner_waits": (
        "repo TEXT, check_name TEXT, event TEXT, wait_secs REAL, started_at TEXT, pool TEXT"
    ),
}


def default_dest() -> str:
    return str(Path.home() / ".claude-accounts" / "pipeline_history.db")


def migrate_history(source_path: str = SOURCE_DEFAULT, dest_path: str | None = None) -> dict[str, int]:
    dest_path = dest_path or default_dest()
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    dest = sqlite3.connect(dest_path)
    counts: dict[str, int] = {}
    try:
        for table, schema in _SCHEMA.items():
            cols = [c.split()[0] for c in schema.split(",")]
            dest.execute(f"DROP TABLE IF EXISTS {table}")
            dest.execute(f"CREATE TABLE {table} ({schema})")
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            placeholders = ",".join("?" for _ in cols)
            dest.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows)
            counts[table] = len(rows)
        # Migrate the meta table's ciGraph entries so effective_prefixes() can read
        # the oracle's derived required-check prefixes (BFS closure of the rollup
        # job's needs in ci.yml).  Only ciGraph:* keys are needed; other meta keys
        # (lastSweep, backfilled, …) are operational and not consumed by ccswitch.
        _migrate_meta(src, dest)
        # Indexes mirroring history.ts (speed the read helpers).
        dest.execute("CREATE INDEX IF NOT EXISTS idx_check_durations "
                     "ON check_durations(repo, check_name, event, completed_at)")
        dest.execute("CREATE INDEX IF NOT EXISTS idx_runner_waits "
                     "ON runner_waits(repo, check_name, event, started_at)")
        dest.commit()
    finally:
        src.close(); dest.close()
    return counts


def _migrate_meta(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """Copy ciGraph:* rows from the source meta table (if it exists) into dest."""
    has_meta = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        return
    dest.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows = src.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'ciGraph:%'"
    ).fetchall()
    dest.execute("DELETE FROM meta WHERE key LIKE 'ciGraph:%'")
    dest.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", rows)
