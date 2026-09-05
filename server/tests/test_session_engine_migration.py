"""Managed and runtime migration coverage for engine-neutral session identity."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "db/migrations/versions/0024_session_engine.py"
    )
    spec = importlib.util.spec_from_file_location("session_engine_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_sqlite_upgrade_preserves_id_and_unrelated_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'managed.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, claude_session_id TEXT)"
        ))
        conn.execute(text("CREATE INDEX ix_sessions_name_test ON sessions (name)"))
        conn.execute(text(
            "INSERT INTO sessions (id, name, claude_session_id) "
            "VALUES ('s1', 'one', 'native:exact/value')"
        ))

        migration = _migration_module()
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()  # defensive idempotence for partially managed DBs

        columns = {column["name"] for column in inspect(conn).get_columns("sessions")}
        assert {"engine", "engine_session_id", "reasoning_effort"} <= columns
        assert "claude_session_id" not in columns
        assert "ix_sessions_name_test" in {
            index["name"] for index in inspect(conn).get_indexes("sessions")
        }
        row = conn.execute(text(
            "SELECT engine, engine_session_id, reasoning_effort FROM sessions WHERE id='s1'"
        )).one()
        assert row == ("claude_code", "native:exact/value", None)


def test_session_orm_has_only_canonical_native_id():
    from server.db.models import Session

    columns = Session.__table__.columns
    assert columns["engine"].nullable
    assert columns["engine_session_id"].nullable
    assert columns["reasoning_effort"].nullable
    assert "claude_session_id" not in columns
