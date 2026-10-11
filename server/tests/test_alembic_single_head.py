"""The migration graph has exactly ONE head (#2683 review).

Two branches that each add ``00NN`` off the same parent merge without a textual
conflict — different filenames — and leave alembic with two heads, which nothing at
deploy time notices (reconcile, not ``alembic upgrade``, applies schema changes). A
duplicate revision id is the same mistake, caught earlier. This pins both, from the
revision files alone (no database, no ``env.py``).
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

VERSIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def _script() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(VERSIONS))
    return ScriptDirectory.from_config(cfg)


def test_the_migration_graph_has_a_single_head():
    heads = _script().get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads {sorted(heads)}: chain the newest revision's "
        "down_revision off the current head instead of a shared parent")


def test_every_revision_id_is_unique_and_reachable_from_the_head():
    script = _script()
    files = [p for p in (VERSIONS / "versions").glob("*.py") if not p.name.startswith("_")]
    revisions = list(script.walk_revisions())
    assert len(revisions) == len(files), (
        "a revision file is unreachable from the head or two files share a revision id")
