"""Phase 2c: automation dedupe keys converge off the pre-rebrand product slug (#1369).

The SQL is generated once in ``server/db/brand_keys`` and driven by BOTH the alembic
revision ``0061_brand_automation_keys`` (the reviewable record) and
``scripts/rename_automation_keys_brand.py`` (what actually runs — nothing runs
``alembic upgrade`` at deploy time). These tests exercise the generated statements
against a real database, so a dialect mistake in either consumer surfaces here rather
than in the maintenance window.
"""
import importlib.util
import itertools
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from server.db import db_session
from server.db.brand_keys import (
    KEY_COLUMNS,
    KEY_PREFIX,
    LEGACY_KEY_PREFIX,
    delete_collisions_sql,
    rewrite_key,
    select_legacy_sql,
    update_sql,
)
from server.db.models import Base

MIGRATION = Path("server/db/migrations/versions/0061_brand_automation_keys.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0061", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Distinct per placeholder string, so two rows in one table never collide on the
#: primary key the tests do not otherwise care about.
_seq = itertools.count()


def _placeholder(column):
    """Something valid for a NOT NULL column whose value this test does not care about.

    Derived from the column's type rather than a hand-listed set of required columns
    per table: a key column added to a NEW table then needs no bookkeeping here, which
    is the point of driving the tests off KEY_COLUMNS at all.
    """
    kind = column.type
    if isinstance(kind, sa.DateTime):
        return datetime(2026, 9, 23, 12, 0, 0)
    if isinstance(kind, sa.Boolean):
        return False
    if isinstance(kind, sa.Integer):
        return 0
    if isinstance(kind, sa.JSON):
        return {}
    return f"x{next(_seq)}"


async def _insert(db, col, value, **overrides):
    """One row in *col*'s table carrying *value* in the key column."""
    table = Base.metadata.tables[col.table]
    values = {c.name: _placeholder(c) for c in table.columns if not c.nullable}
    values[col.column] = value
    values.update(overrides)
    await db.execute(sa.insert(table).values(**values))


async def _converge(db):
    """What both consumers do, in their order: collisions first, then the UPDATE."""
    for col in KEY_COLUMNS:
        collisions = delete_collisions_sql(col)
        if collisions:
            await db.execute(text(collisions))
        await db.execute(text(update_sql(col)))


async def _count_legacy(db, col):
    return int((await db.execute(text(select_legacy_sql(col)))).scalar() or 0)


async def _keys(db, col):
    return sorted(
        r[0] for r in
        (await db.execute(sa.select(Base.metadata.tables[col.table].c[col.column]))).all()
    )


# --------------------------------------------------------------------------- #
# the record                                                                   #
# --------------------------------------------------------------------------- #

def test_revision_is_chained_off_the_newest():
    module = _load_migration()
    assert module.revision == "0061_brand_automation_keys"
    assert module.down_revision == "0060_backlog_groups_degraded"


def test_downgrade_refuses_rather_than_re_splitting_the_key():
    """A reverse migration would re-create the split identity this removes, and it
    cannot bring back a row the collision delete reaped. Refusing loudly beats a
    downgrade that silently re-introduces duplicate spawns."""
    with pytest.raises(NotImplementedError):
        _load_migration().downgrade()


# --------------------------------------------------------------------------- #
# the rewrite itself                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    (f"{LEGACY_KEY_PREFIX}intent", f"{KEY_PREFIX}intent"),
    (f"{LEGACY_KEY_PREFIX}map-diff:billing", f"{KEY_PREFIX}map-diff:billing"),
    # Already converged, and keys that were never ours: untouched.
    (f"{KEY_PREFIX}intent", f"{KEY_PREFIX}intent"),
    ("kindash:intent", "kindash:intent"),
    ("steiner385/viberpit#42", "steiner385/viberpit#42"),
    # LEADING slug only. `replace()` — which the spec sketched — would rewrite this
    # one in the middle and mint a key nothing composes.
    (f"pr-doctor:{LEGACY_KEY_PREFIX}7", f"pr-doctor:{LEGACY_KEY_PREFIX}7"),
    (None, None),
])
def test_rewrite_key_converges_only_a_leading_product_slug(value, expected):
    assert rewrite_key(value) == expected


def test_the_sql_and_the_pure_rewrite_agree_on_the_prefix():
    """``rewrite_key`` is the readable definition; the UPDATE is what runs. Pin the
    one thing they could disagree on — where the legacy prefix ends."""
    assert rewrite_key(f"{LEGACY_KEY_PREFIX}x") == f"{KEY_PREFIX}x"
    # substr() is 1-based, so the offset is len(prefix) + 1; an off-by-one here would
    # keep or eat a character and the tests below would still "converge" something.
    assert f"substr(origin_ref, {len(LEGACY_KEY_PREFIX) + 1})" in update_sql(KEY_COLUMNS[0])


# --------------------------------------------------------------------------- #
# against a real database                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_every_key_column_converges(isolated_db):
    """One legacy row per column in KEY_COLUMNS, all converged in one pass.

    Driven off KEY_COLUMNS rather than a hand-listed set, so a column added to the key
    space without a working statement fails here instead of being quietly skipped."""
    async with db_session() as db:
        for col in KEY_COLUMNS:
            await _insert(db, col, f"{LEGACY_KEY_PREFIX}intent")
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        for col in KEY_COLUMNS:
            assert await _count_legacy(db, col) == 0, col
            assert await _keys(db, col) == [f"{KEY_PREFIX}intent"], col


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing(isolated_db):
    """The issue's exit criterion. The second pass must be a no-op — not a double
    rewrite (``viberpit:viberpit:intent``) and not an error."""
    sessions = KEY_COLUMNS[0]
    async with db_session() as db:
        await _insert(db, sessions, f"{LEGACY_KEY_PREFIX}intent")
        await db.commit()

    for _ in range(2):
        async with db_session() as db:
            await _converge(db)
            await db.commit()

    async with db_session() as db:
        assert await _keys(db, sessions) == [f"{KEY_PREFIX}intent"]


@pytest.mark.asyncio
async def test_a_key_that_was_never_ours_is_left_alone(isolated_db):
    """Another product's keys share the column, and a key that merely CONTAINS the
    legacy word is not ours either. The statement has to be able to refuse, or the
    tests above prove nothing."""
    sessions = KEY_COLUMNS[0]
    untouched = ["kindash:intent", f"pr-doctor:{LEGACY_KEY_PREFIX}7"]
    async with db_session() as db:
        for value in untouched:
            await _insert(db, sessions, value)
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        assert await _keys(db, sessions) == sorted(untouched)


@pytest.mark.asyncio
async def test_only_the_leading_slug_is_rewritten_inside_a_row(isolated_db):
    """The case that separates the prefix rewrite from ``replace()`` in the database.

    A key that starts with the legacy slug AND carries it again later is selected by
    the anchored WHERE, so the SET expression is free to corrupt the tail: ``replace()``
    would rewrite BOTH occurrences and mint a key the emitter never composes. Every
    other row makes the two implementations look identical, which is exactly why this
    one has to exist.
    """
    sessions = KEY_COLUMNS[0]
    doubled = f"{LEGACY_KEY_PREFIX}map-diff:{LEGACY_KEY_PREFIX}ui"
    async with db_session() as db:
        await _insert(db, sessions, doubled)
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        assert await _keys(db, sessions) == [f"{KEY_PREFIX}map-diff:{LEGACY_KEY_PREFIX}ui"]


@pytest.mark.asyncio
async def test_a_collision_drops_the_stale_row_instead_of_aborting(isolated_db):
    """``automation_target_state.target`` is half a PRIMARY KEY.

    When both spellings exist for one automation, the plain UPDATE would raise a
    uniqueness violation and abandon every column after it. The converged row is the
    live one — it is the key the emitter composes now — so the legacy row goes."""
    state = next(c for c in KEY_COLUMNS if c.unique_with)
    async with db_session() as db:
        await _insert(db, state, f"{LEGACY_KEY_PREFIX}intent",
                      automation_id="sdlc-intake", state={"tries": 9})
        await _insert(db, state, f"{KEY_PREFIX}intent",
                      automation_id="sdlc-intake", state={"tries": 1})
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        rows = (await db.execute(text(
            "SELECT target, state FROM automation_target_state "
            "WHERE automation_id = 'sdlc-intake'"))).all()
    assert [r[0] for r in rows] == [f"{KEY_PREFIX}intent"]
    assert "1" in str(rows[0][1]) and "9" not in str(rows[0][1])  # live counters kept


@pytest.mark.asyncio
async def test_the_same_key_under_another_automation_is_not_a_collision(isolated_db):
    """Uniqueness is per ``(automation_id, target)``. The same key under a DIFFERENT
    automation must be converged, not deleted as a duplicate — a collision delete that
    ignored ``unique_with`` would silently drop another automation's counters."""
    state = next(c for c in KEY_COLUMNS if c.unique_with)
    async with db_session() as db:
        await _insert(db, state, f"{LEGACY_KEY_PREFIX}intent", automation_id="sdlc-intake")
        await _insert(db, state, f"{KEY_PREFIX}intent", automation_id="pm-classify")
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        rows = (await db.execute(text(
            "SELECT automation_id, target FROM automation_target_state "
            "ORDER BY automation_id"))).all()
    assert [tuple(r) for r in rows] == [
        ("pm-classify", f"{KEY_PREFIX}intent"),
        ("sdlc-intake", f"{KEY_PREFIX}intent"),
    ]


# --------------------------------------------------------------------------- #
# the one-shot's database guard                                                #
# --------------------------------------------------------------------------- #

def _load_script():
    path = Path("scripts/rename_automation_keys_brand.py")
    spec = importlib.util.spec_from_file_location("rename_automation_keys_brand", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_run_refuses_when_no_database_is_named():
    """Unset, the settings default is an empty local SQLite file — in which there are
    no rows at all, so the run would report a clean all-clear on the wrong database."""
    assert _load_script().db_url_error(None, "sqlite+aiosqlite:///./x.db")


def test_the_run_refuses_a_database_the_settings_did_not_resolve():
    """The trap this script actually hit: `mirror_legacy_env` does not overwrite, so a
    legacy-prefixed DB URL already exported by the host's env file WINS, and the run
    would write to the live cluster while the operator believed they had pointed it at
    a scratch file."""
    error = _load_script().db_url_error(
        "sqlite+aiosqlite:////tmp/scratch.db",
        "postgresql+asyncpg://u:pw@10.0.0.1:5432/live")
    assert error and "different database" in error
    assert "pw" not in error, "the guard must not print the credentials half"


def test_the_run_proceeds_when_they_agree():
    """The guard has to be able to PASS, or the two refusals above prove nothing."""
    url = "postgresql+asyncpg://u:pw@10.0.0.1:5432/live"
    assert _load_script().db_url_error(url, url) is None
