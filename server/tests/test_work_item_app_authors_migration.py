"""Migration 0081: only the two verified-unregistered App logins gain ``app/``, only
for rows created before 2026-10-10 and last synced before the run, and it records the
one-shot ``data_backfills`` marker the startup twin checks."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import DateTime, bindparam, create_engine, inspect, text

MIGRATION = (Path(__file__).resolve().parents[1]
             / "db/migrations/versions/0082_work_item_app_authors.py")

_BEFORE = datetime(2026, 9, 1, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 11, tzinfo=timezone.utc)
_FUTURE = datetime.now(timezone.utc) + timedelta(days=1)


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


def test_revision_chains_after_0081():
    mod = _module()
    assert mod.revision == "0082_work_item_app_authors"
    assert mod.down_revision == "0081_drop_test_run_slots"


def test_upgrade_converts_only_old_app_rows_marks_itself_and_downgrade_reverses(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    rows = [("github-actions", _BEFORE, _BEFORE), ("viberpit", _BEFORE, _BEFORE),
            ("github-actions", _AFTER, _BEFORE), ("viberpit", _BEFORE, _FUTURE),
            ("copilot-swe-agent", _BEFORE, _BEFORE), ("tony", _BEFORE, _BEFORE),
            (None, _BEFORE, _BEFORE)]
    stamp = DateTime(timezone=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id INTEGER PRIMARY KEY, author TEXT, "
            "gh_created_at DATETIME, last_synced_at DATETIME)"))
        for a, created, synced in rows:
            conn.execute(text(
                "INSERT INTO work_items (author, gh_created_at, last_synced_at) "
                "VALUES (:a, :c, :s)").bindparams(
                    bindparam("c", type_=stamp), bindparam("s", type_=stamp)),
                {"a": a, "c": created, "s": synced})
    mod = _module()

    def read():
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT author FROM work_items ORDER BY id"))]

    _run(engine, mod.upgrade)
    assert read() == ["app/github-actions", "app/viberpit", "github-actions", "viberpit",
                      "copilot-swe-agent", "tony", None]
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT name FROM data_backfills"))] == [
            "work_item_app_authors"]
    _run(engine, mod.downgrade)
    assert read() == [a for a, _, _ in rows]
    assert "data_backfills" not in inspect(engine).get_table_names()
