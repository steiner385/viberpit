"""Migration 0052: ``sessions.pr_resolution_note`` is added, guarded against a
second run (spec 2026-09-18 §5)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = Path("server/db/migrations/versions/0052_pr_resolution_note.py")


def _module():
    spec = importlib.util.spec_from_file_location("pr_resolution_note_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_adds_pr_resolution_note_column_and_is_guarded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, pr_resolution TEXT, "
            "pr_resolution_source TEXT)"))
    mod = _module()
    assert mod.down_revision == "0051_pr_resolution_source_observed", "chained off the newest revision"
    for _ in range(2):  # a second run must not error (guarded add-column idiom)
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                mod.upgrade()
        cols = {c["name"] for c in inspect(engine).get_columns("sessions")}
        assert "pr_resolution_note" in cols
