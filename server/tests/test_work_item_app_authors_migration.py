"""Migration 0080: only the two verified-unregistered App logins gain ``app/``."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

MIGRATION = Path("server/db/migrations/versions/0080_work_item_app_authors.py")


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


def test_upgrade_converts_only_the_two_apps_and_downgrade_reverses(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    authors = ["github-actions", "viberpit", "copilot-swe-agent", "tony", None]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work_items (id INTEGER PRIMARY KEY, author TEXT)"))
        for a in authors:
            conn.execute(text("INSERT INTO work_items (author) VALUES (:a)"), {"a": a})
    mod = _module()

    def read():
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT author FROM work_items ORDER BY id"))]

    _run(engine, mod.upgrade)
    assert read() == ["app/github-actions", "app/viberpit", "copilot-swe-agent", "tony", None]
    _run(engine, mod.downgrade)
    assert read() == authors
