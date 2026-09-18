"""Migration 0050: ``dispatch_outcomes.dispatch_kind`` is added, guarded against a
second run (spec 2026-09-18 §3/§5)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = Path("server/db/migrations/versions/0050_dispatch_kind.py")


def _module():
    spec = importlib.util.spec_from_file_location("dispatch_kind_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_adds_dispatch_kind_column_and_is_guarded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE dispatch_outcomes (id TEXT PRIMARY KEY, target_ref TEXT, "
            "body_fingerprint TEXT, outcome TEXT)"))
    mod = _module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
    cols = {c["name"] for c in inspect(engine).get_columns("dispatch_outcomes")}
    assert "dispatch_kind" in cols

    # Running it again must not error (guarded add-column idiom).
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
    cols = {c["name"] for c in inspect(engine).get_columns("dispatch_outcomes")}
    assert "dispatch_kind" in cols
