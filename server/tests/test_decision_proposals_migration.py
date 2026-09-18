"""Migration 0052: the decision_proposals table exists and the revision is re-runnable.

Nothing runs ``alembic upgrade`` at deploy time — ``create_all`` builds the table
at next boot — so the revision's job is to be the reviewable record AND to survive
being applied to a database that already has the table.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = Path("server/db/migrations/versions/0052_decision_proposals.py")

EXPECTED_COLUMNS = {
    "id", "work_item_id", "session_id", "options", "recommended_key", "rationale",
    "chosen_key", "status", "proposed_by", "resolved_by", "created_at", "resolved_at",
}


def _module():
    spec = importlib.util.spec_from_file_location("decision_proposals_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, "
                          "number INTEGER, kind TEXT)"))
    return engine


def _index_sql(engine, name: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"),
                            {"n": name}).scalar_one()


def _upgrade(engine, mod):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()


def test_upgrade_creates_the_table_and_its_indexes(tmp_path):
    engine = _engine(tmp_path)
    mod = _module()
    _upgrade(engine, mod)
    inspector = inspect(engine)
    assert mod.TABLE in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns(mod.TABLE)}
    assert cols == EXPECTED_COLUMNS
    # everything past the PK is nullable, so reconcile can ALTER-ADD a later column
    nullable = {c["name"] for c in inspector.get_columns(mod.TABLE) if c["nullable"]}
    assert EXPECTED_COLUMNS - nullable == {"id", "created_at"}
    indexes = {idx["name"]: idx for idx in inspector.get_indexes(mod.TABLE)}
    assert {mod.WORK_ITEM_INDEX, mod.STATUS_INDEX, mod.PENDING_INDEX} <= set(indexes)
    pending = indexes[mod.PENDING_INDEX]
    assert pending["unique"] and pending["column_names"] == ["work_item_id"]
    # partial, so an item may be decided many times over its life
    assert "WHERE status = 'pending'" in _index_sql(engine, mod.PENDING_INDEX)


def test_upgrade_is_a_no_op_on_a_database_that_already_has_the_table(tmp_path):
    """create_all builds the table at boot, so the revision meets an existing one."""
    engine = _engine(tmp_path)
    mod = _module()
    _upgrade(engine, mod)
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {mod.TABLE} (id, status, created_at) "
            "VALUES ('keep-me', 'pending', :at)"),
            {"at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ")})
    _upgrade(engine, mod)     # guarded twice: table AND indexes
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT id, status FROM {mod.TABLE}")).all()
    assert rows == [("keep-me", "pending")]


def test_model_and_migration_agree_on_the_columns():
    """The revision is the record of what create_all builds — one drifting from
    the other is how a column reaches production on only one dialect."""
    from server.db.models import DecisionProposal
    assert {c.name for c in DecisionProposal.__table__.columns} == EXPECTED_COLUMNS
    # create_all builds the indexes off the model; the revision must name the same
    # ones, or a database built by one route lacks a guard the other has.
    mod = _module()
    assert {i.name for i in DecisionProposal.__table__.indexes} == {
        mod.WORK_ITEM_INDEX, mod.STATUS_INDEX, mod.PENDING_INDEX}
