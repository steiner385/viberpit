"""One-shot script for legacy needs_human conversion (spec 2026-09-18 §3)."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

SCRIPT = Path("scripts/convert_legacy_needs_human.py")


def _module():
    spec = importlib.util.spec_from_file_location("convert_legacy_needs_human", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_script_dry_run_leaves_rows_unconverted(tmp_path):
    """Dry run reports count but does not convert."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/c.db"
    engine = create_engine(db_url.replace("sqlite+aiosqlite", "sqlite"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, "
            "kind TEXT, body TEXT, triage_state TEXT, classified_by TEXT, classified_at DATETIME, "
            "reach INTEGER, impact INTEGER, effort INTEGER, confidence INTEGER, "
            "hold_kind TEXT, hold_reason TEXT, hold_by TEXT, hold_set_at DATETIME, hold_fingerprint TEXT)"))
        conn.execute(text(
            "INSERT INTO work_items (id, repo, number, kind, triage_state, classified_by, classified_at, "
            "reach, impact, effort, confidence) VALUES "
            "('row1', 'o/r', 1, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', 2, 2, 2, 2)"))
    mod = _module()
    result = asyncio.run(mod.main(apply=False, db_url=db_url))
    assert result == 0
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT triage_state, hold_kind FROM work_items WHERE id = 'row1'")).first()
    assert row[0] == "needs_human"
    assert row[1] is None


def test_script_apply_converts_rows(tmp_path):
    """Apply mode converts rows."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/c.db"
    engine = create_engine(db_url.replace("sqlite+aiosqlite", "sqlite"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, "
            "kind TEXT, body TEXT, triage_state TEXT, classified_by TEXT, classified_at DATETIME, "
            "reach INTEGER, impact INTEGER, effort INTEGER, confidence INTEGER, "
            "hold_kind TEXT, hold_reason TEXT, hold_by TEXT, hold_set_at DATETIME, hold_fingerprint TEXT)"))
        conn.execute(text(
            "INSERT INTO work_items (id, repo, number, kind, triage_state, classified_by, classified_at, "
            "reach, impact, effort, confidence) VALUES "
            "('row1', 'o/r', 1, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', 2, 2, 2, 2)"))
    mod = _module()
    result = asyncio.run(mod.main(apply=True, db_url=db_url))
    assert result == 0
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT triage_state, hold_kind, hold_by, hold_reason FROM work_items WHERE id = 'row1'")).first()
    assert row[0] == "classified"
    assert row[1] == "owner_action"
    assert row[2] == "migration"
    assert row[3] == "pre-ladder needs_human"
    # ``WorkItem.hold_kind`` declares index=True, but reconcile ALTER-ADDs COLUMNS only
    # and nothing runs `alembic upgrade` at deploy — so this script is what puts the
    # index on an existing DB (final-review M10).
    from server.pm.holds import HOLD_INDEX_NAME
    assert HOLD_INDEX_NAME in {
        i["name"] for i in inspect(engine).get_indexes("work_items")}
