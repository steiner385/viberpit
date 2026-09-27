"""The SDLC control-band id converges off the pre-rebrand product slug (epic #1365).

``server/sdlc/bands.yaml`` names the PR-cycle-time band ``<product-slug>-pr-cycle-time``
and the monitor persists that id into every triage row it files: ``metric``, the row's
unique ``key`` (``<metric>:<rule>:<side>``) and the first word of ``summary``. The SQL
is generated once in ``server/db/brand_keys`` (``BAND_ID_COLUMNS``) and driven by BOTH
the alembic revision ``0074_brand_control_band_id`` (the reviewable record) and
``scripts/rename_automation_keys_brand.py`` (what actually runs). These tests exercise
the generated statements against a real database, the same way
``test_automation_keys_migration.py`` does for the dedupe keys.
"""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from server.db import db_session
from server.db.brand_keys import (
    ALL_KEY_COLUMNS,
    BAND_ID_COLUMNS,
    BAND_ID_PREFIX,
    KEY_COLUMNS,
    LEGACY_BAND_ID_PREFIX,
    delete_collisions_sql,
    rewrite_key,
    select_legacy_sql,
    update_sql,
)
from server.db.models import Base, TriageItem
from server.sdlc.bands import load_bands
from server.sdlc.control_bands import Baseline, Breach
from viberpit import brand

MIGRATION = Path("server/db/migrations/versions/0074_brand_control_band_id.py")

BAND = f"{BAND_ID_PREFIX}pr-cycle-time"
LEGACY_BAND = f"{LEGACY_BAND_ID_PREFIX}pr-cycle-time"


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0074", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(band: str, **overrides) -> TriageItem:
    """A triage row the way ``upsert_breach`` files it for *band*."""
    values = dict(
        key=f"{band}:we1:high", metric=band, source="pr_cycle_time_hours",
        repo="steiner385/viberpit", rule="we1", sigma=3, side="high",
        tier="propose", response="propose", value=50.0, baseline_mean=10.0,
        baseline_stddev=0.5, baseline_n=30, observed_at="2026-09-25",
        summary=f"{band} above 3σ (we1): 50 vs baseline 10 ±0.5 (n=30) at 2026-09-25",
        evidence=[], status="open", occurrences=1,
    )
    values.update(overrides)
    return TriageItem(**values)


async def _converge(db):
    """What both consumers do, in their order: collisions first, then the UPDATE."""
    for col in BAND_ID_COLUMNS:
        collisions = delete_collisions_sql(col)
        if collisions:
            await db.execute(text(collisions))
        await db.execute(text(update_sql(col)))


async def _rows(db) -> list[tuple]:
    return [tuple(r) for r in (await db.execute(text(
        "SELECT key, metric, summary, status, occurrences FROM triage_items "
        "ORDER BY key"))).all()]


# --------------------------------------------------------------------------- #
# the record                                                                   #
# --------------------------------------------------------------------------- #

def test_revision_is_chained_off_the_newest():
    module = _load_migration()
    assert module.revision == "0074_brand_control_band_id"
    assert module.down_revision == "0073_dispatch_outcomes_session_fk_set_null"


def test_downgrade_refuses_rather_than_re_splitting_the_incident():
    with pytest.raises(NotImplementedError):
        _load_migration().downgrade()


def test_the_operator_script_walks_the_band_columns_too():
    """The record and the run share one statement list; the script must not have
    kept the Phase 2c tuple and silently skipped the band columns."""
    assert set(BAND_ID_COLUMNS) <= set(ALL_KEY_COLUMNS)
    assert set(KEY_COLUMNS) <= set(ALL_KEY_COLUMNS)


# --------------------------------------------------------------------------- #
# the emitter                                                                  #
# --------------------------------------------------------------------------- #

def test_the_committed_band_id_carries_the_current_slug():
    """The yaml is the emitter. If it lags the slug, every test below converges rows
    towards an id nothing files any more."""
    ids = [m.id for m in load_bands().metrics]
    assert BAND in ids
    assert not any(m.startswith(LEGACY_BAND_ID_PREFIX) for m in ids)
    assert BAND_ID_PREFIX == f"{brand.SLUG}-"


def test_the_persisted_key_and_summary_open_with_the_band_id():
    """Pins the shape the SQL relies on: ``key`` and ``summary`` START with the metric
    id, so a leading-prefix rewrite converges them and nothing else."""
    breach = Breach(metric=BAND, rule="we1", sigma=3, side="high", at="2026-09-25",
                    value=50.0, baseline=Baseline(mean=10.0, stddev=0.5, n=30),
                    points=())
    assert breach.key().startswith(f"{BAND}:")
    assert breach.summary().startswith(f"{BAND} ")


# --------------------------------------------------------------------------- #
# the rewrite itself                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    (f"{LEGACY_BAND}:we1:high", f"{BAND}:we1:high"),
    (LEGACY_BAND, BAND),
    (f"{LEGACY_BAND} above 3σ", f"{BAND} above 3σ"),
    # Already converged, another band, and one that merely CONTAINS the slug.
    (f"{BAND}:we1:high", f"{BAND}:we1:high"),
    (f"{BAND_ID_PREFIX}ci-failure-rate", f"{BAND_ID_PREFIX}ci-failure-rate"),
    (f"kindash-{LEGACY_BAND}", f"kindash-{LEGACY_BAND}"),
    (None, None),
])
def test_rewrite_key_converges_only_a_leading_band_slug(value, expected):
    assert rewrite_key(value, LEGACY_BAND_ID_PREFIX, BAND_ID_PREFIX) == expected


def test_the_sql_and_the_pure_rewrite_agree_on_the_prefix():
    key_col = BAND_ID_COLUMNS[0]
    assert key_col.column == "key" and key_col.unique and key_col.keep == "legacy"
    assert f"substr(key, {len(LEGACY_BAND_ID_PREFIX) + 1})" in update_sql(key_col)
    assert f"LIKE '{LEGACY_BAND_ID_PREFIX}%'" in select_legacy_sql(key_col)
    # The free-form columns never collide, so they get no delete at all.
    assert all(delete_collisions_sql(c) is None for c in BAND_ID_COLUMNS[1:])


# --------------------------------------------------------------------------- #
# against a real database                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_legacy_incident_keeps_its_disposition_under_the_new_id(isolated_db):
    """The production case: one row, filed under the old id and already adopted by
    Intake. It must come out with the new key, metric and summary and the SAME
    disposition — the whole point is that the next tick refreshes it instead of
    filing a second, open incident."""
    async with db_session() as db:
        db.add(_row(LEGACY_BAND, status="intake", initiative_slug="incident-cycle-time",
                    occurrences=4))
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        assert await _rows(db) == [(
            f"{BAND}:we1:high", BAND,
            f"{BAND} above 3σ (we1): 50 vs baseline 10 ±0.5 (n=30) at 2026-09-25",
            "intake", 4)]
        for col in BAND_ID_COLUMNS:
            assert (await db.execute(text(select_legacy_sql(col)))).scalar() == 0, col


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing(isolated_db):
    async with db_session() as db:
        db.add(_row(LEGACY_BAND))
        await db.commit()

    for _ in range(2):
        async with db_session() as db:
            await _converge(db)
            await db.commit()

    async with db_session() as db:
        rows = await _rows(db)
    assert [r[0] for r in rows] == [f"{BAND}:we1:high"]
    assert rows[0][1] == BAND


@pytest.mark.asyncio
async def test_a_collision_keeps_the_audit_record_and_drops_the_re_detection(isolated_db):
    """``triage_items.key`` is unique on its own. Between the yaml flip and this
    converge, a tick can file the SAME breach under the new id as a fresh open row.
    The opposite of the dedupe-key policy applies: the legacy row is the incident a
    human or Intake already dispositioned, so IT survives and the twin goes."""
    async with db_session() as db:
        db.add(_row(LEGACY_BAND, status="intake", occurrences=4))
        db.add(_row(BAND, status="open", occurrences=1))
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        assert await _rows(db) == [(
            f"{BAND}:we1:high", BAND,
            f"{BAND} above 3σ (we1): 50 vs baseline 10 ±0.5 (n=30) at 2026-09-25",
            "intake", 4)]


@pytest.mark.asyncio
async def test_another_band_and_another_side_are_not_collisions(isolated_db):
    """Only an exact twin under the other spelling is a duplicate. A different band
    with the same prefix, and the same band on the other side, both stay."""
    async with db_session() as db:
        db.add(_row(LEGACY_BAND))
        db.add(_row(BAND, key=f"{BAND}:we1:low", side="low"))
        db.add(_row(f"{BAND_ID_PREFIX}ci-failure-rate"))
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        keys = [r[0] for r in await _rows(db)]
    assert keys == sorted([
        f"{BAND}:we1:high", f"{BAND}:we1:low", f"{BAND_ID_PREFIX}ci-failure-rate:we1:high",
    ])


@pytest.mark.asyncio
async def test_the_dedupe_key_columns_are_untouched_by_the_band_statements(isolated_db):
    """A ``<slug>:`` dedupe key and a ``<slug>-`` band id share a slug, not a prefix:
    the band statements must not select a dedupe-key row, and vice versa."""
    sessions = Base.metadata.tables["sessions"]
    async with db_session() as db:
        # A sessions row with only what NOT NULL demands, plus the legacy dedupe key.
        values = {c.name: "x" for c in sessions.columns if not c.nullable
                  and not isinstance(c.type, (sa.DateTime, sa.Boolean, sa.Integer, sa.JSON))}
        values["origin_ref"] = f"{brand.LEGACY_SLUG}:intent"
        await db.execute(sa.insert(sessions).values(**values))
        await db.commit()

    async with db_session() as db:
        await _converge(db)
        await db.commit()

    async with db_session() as db:
        left = (await db.execute(
            sa.select(sessions.c.origin_ref))).scalars().all()
    assert left == [f"{brand.LEGACY_SLUG}:intent"]
