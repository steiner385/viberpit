"""Migration 0087: ``owner_recommendations`` + ``attention_samples`` exist, match the
models, and the revision is re-runnable (``create_all`` may have built them first)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from server.db.models import AttentionSample, OwnerRecommendation

MIGRATION = Path("server/db/migrations/versions/0087_needs_you_hygiene.py")


def _module():
    spec = importlib.util.spec_from_file_location("needs_you_hygiene_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _upgrade(engine, mod):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()


def test_upgrade_creates_both_tables_like_the_models_and_reruns_cleanly(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    mod = _module()
    _upgrade(engine, mod)
    _upgrade(engine, mod)          # a second run (or one after create_all) is a no-op
    insp = inspect(engine)
    for model, index in ((OwnerRecommendation, mod.RECS_INDEX),
                         (AttentionSample, mod.SAMPLES_INDEX)):
        table = model.__tablename__
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert set(cols) == {c.name for c in model.__table__.columns}
        assert {n: c["nullable"] for n, c in cols.items()} == {
            c.name: bool(c.nullable) for c in model.__table__.columns}
        assert index in {i["name"] for i in insp.get_indexes(table)}
        assert index in {i.name for i in model.__table__.indexes}
