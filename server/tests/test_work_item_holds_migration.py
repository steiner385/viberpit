"""Migration 0049: hold columns exist and legacy needs_human rows convert (spec §3)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = Path("server/db/migrations/versions/0049_work_item_holds.py")


def _module():
    spec = importlib.util.spec_from_file_location("work_item_holds_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_adds_columns_and_converts_legacy_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, "
            "kind TEXT, body TEXT, triage_state TEXT, classified_by TEXT, classified_at DATETIME, "
            "reach INTEGER, impact INTEGER, effort INTEGER, confidence INTEGER)"))
        conn.execute(text(
            "INSERT INTO work_items (id, repo, number, kind, triage_state, classified_by, classified_at, "
            "reach, impact, effort, confidence) VALUES "
            "('scored', 'o/r', 1, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', 2, 2, 2, 2), "
            "('unscored', 'o/r', 2, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', NULL, 2, 2, 2), "
            "('untouched', 'o/r', 3, 'issue', 'classified', 'auto', '2026-09-12 18:43:51', 1, 1, 1, 1)"))
    mod = _module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
    cols = {c["name"] for c in inspect(engine).get_columns("work_items")}
    assert {"hold_kind", "hold_reason", "hold_by", "hold_set_at", "hold_fingerprint"} <= cols
    with engine.connect() as conn:
        rows = {r[0]: r for r in conn.execute(text(
            "SELECT id, triage_state, hold_kind, hold_by, hold_reason, hold_set_at FROM work_items"))}
    assert rows["scored"][1:5] == ("classified", "owner_action", "migration", "pre-ladder needs_human")
    assert rows["scored"][5] is not None
    assert rows["unscored"][1:3] == ("unclassified", "owner_action")
    assert rows["untouched"][1:3] == ("classified", None)


def test_upgrade_does_not_overwrite_existing_holds(tmp_path):
    """Rows already carrying a hold are not overwritten (hold_kind IS NULL guard)."""
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, "
            "kind TEXT, body TEXT, triage_state TEXT, classified_by TEXT, classified_at DATETIME, "
            "reach INTEGER, impact INTEGER, effort INTEGER, confidence INTEGER, "
            "hold_kind TEXT, hold_reason TEXT, hold_by TEXT, hold_set_at DATETIME, hold_fingerprint TEXT)"))
        conn.execute(text(
            "INSERT INTO work_items (id, repo, number, kind, triage_state, classified_by, classified_at, "
            "reach, impact, effort, confidence, hold_kind, hold_by, hold_reason) VALUES "
            "('held', 'o/r', 1, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', NULL, 1, 1, 1, "
            "'evidence', 'session:s9', 'keep me')"))
    mod = _module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT triage_state, hold_kind, hold_by, hold_reason FROM work_items WHERE id = 'held'")).first()
    # triage_state converts per RICE rule (reach is NULL, so not all RICE non-null → unclassified)
    # but hold is preserved
    assert row[0] == "unclassified"
    assert row[1] == "evidence"
    assert row[2] == "session:s9"
    assert row[3] == "keep me"
