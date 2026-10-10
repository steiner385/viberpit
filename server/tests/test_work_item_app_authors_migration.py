"""Migration 0082: bare ``github-actions``/``viberpit`` authors are cleared (never
relabelled ``app/<slug>`` -- a stored login is a stale identity), every repo's mirror
backfill is reset so the new ``__typename``-aware sync re-reads every author, and it
records the one-shot ``data_backfills`` marker the startup twin checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = (Path(__file__).resolve().parents[1]
             / "db/migrations/versions/0082_work_item_app_authors.py")


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


def test_upgrade_clears_bare_app_names_resets_backfill_and_marks_itself(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    authors = ["github-actions", "viberpit", "copilot-swe-agent", "tony",
               "app/github-actions", None]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work_items (id INTEGER PRIMARY KEY, author TEXT)"))
        conn.execute(text(
            "CREATE TABLE project_repos (id TEXT PRIMARY KEY, backfill_done BOOLEAN, "
            "backfill_cursor TEXT, backfill_phase TEXT)"))
        for a in authors:
            conn.execute(text("INSERT INTO work_items (author) VALUES (:a)"), {"a": a})
        conn.execute(text(
            "INSERT INTO project_repos VALUES ('r1', 1, 'CUR', 'prs')"))
    mod = _module()

    def read():
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT author FROM work_items ORDER BY id"))]

    _run(engine, mod.upgrade)
    assert read() == [None, None, "copilot-swe-agent", "tony", "app/github-actions", None]
    with engine.connect() as conn:
        assert tuple(conn.execute(text(
            "SELECT backfill_done, backfill_cursor, backfill_phase FROM project_repos"
        )).one()) == (0, None, "issues")
        assert [r[0] for r in conn.execute(text("SELECT name FROM data_backfills"))] == [
            "work_item_app_authors"]
    # Downgrade drops the marker table; cleared authors are refilled by the re-mirror,
    # not restored (restoring a bare App-looking login is exactly the stale identity).
    _run(engine, mod.downgrade)
    assert "data_backfills" not in inspect(engine).get_table_names()
