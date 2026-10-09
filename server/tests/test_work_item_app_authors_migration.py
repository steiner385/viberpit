"""Migration 0081: only the two verified-unregistered App logins gain ``app/``, and only
for rows created before the 2026-10-10 bound."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import DateTime, bindparam, create_engine, text

MIGRATION = (Path(__file__).resolve().parents[1]
             / "db/migrations/versions/0081_work_item_app_authors.py")

_BEFORE = datetime(2026, 9, 1, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _module():
    spec = importlib.util.spec_from_file_location("work_item_app_authors_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def test_revision_chains_after_0080():
    mod = _module()
    assert mod.revision == "0081_work_item_app_authors"
    assert mod.down_revision == "0080_session_display_fallback_reason"


def test_upgrade_converts_only_pre_bound_app_rows_and_downgrade_reverses(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    rows = [("github-actions", _BEFORE), ("viberpit", _BEFORE), ("github-actions", _AFTER),
            ("copilot-swe-agent", _BEFORE), ("tony", _BEFORE), (None, _BEFORE)]
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id INTEGER PRIMARY KEY, author TEXT, gh_created_at DATETIME)"))
        for a, created in rows:
            conn.execute(text("INSERT INTO work_items (author, gh_created_at) VALUES (:a, :c)")
                         .bindparams(bindparam("c", type_=DateTime(timezone=True))),
                         {"a": a, "c": created})
    mod = _module()
    converted = ["app/github-actions", "app/viberpit", "github-actions",
                 "copilot-swe-agent", "tony", None]

    def read():
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT author FROM work_items ORDER BY id"))]

    _run(engine, mod.upgrade)
    assert read() == converted
    _run(engine, mod.upgrade)  # idempotent
    assert read() == converted
    _run(engine, mod.downgrade)
    assert read() == [a for a, _ in rows]
