"""Release-milestone tables: roundtrip and uniqueness constraints (0103)."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from server.db import db_session
from server.db.models import Milestone, MilestoneLadder, MilestoneMeasurement, Project


async def _ladder() -> str:
    async with db_session() as db:
        p = Project(slug="example", name="Example")
        db.add(p)
        await db.flush()
        lad = MilestoneLadder(project_id=p.id, source_path=".viberpit/milestones.yaml",
                              title="Ladder", definition={"milestones": []})
        db.add(lad)
        await db.commit()
        return lad.id


@pytest.mark.asyncio
async def test_milestone_tables_roundtrip(isolated_db):
    lid = await _ladder()
    async with db_session() as db:
        db.add_all([
            Milestone(ladder_id=lid, key="household", ordinal=0, name="Household",
                      gate=[{"key": "rubric"}], state="current"),
            Milestone(ladder_id=lid, key="friendly", ordinal=1, name="Friendly"),
            MilestoneMeasurement(ladder_id=lid, scope="leaves:household", key="leaves",
                                 value={"done": 1, "total": 2, "items": []}),
        ])
        await db.commit()
    async with db_session() as db:
        m = await db.get(MilestoneLadder, lid)
        assert m is not None and m.refresh_requested_at is None


@pytest.mark.asyncio
async def test_duplicate_milestone_key_rejected(isolated_db):
    lid = await _ladder()
    async with db_session() as db:
        db.add(Milestone(ladder_id=lid, key="a", ordinal=0))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(Milestone(ladder_id=lid, key="a", ordinal=1))
            await db.commit()


@pytest.mark.asyncio
async def test_duplicate_measurement_scope_key_rejected(isolated_db):
    lid = await _ladder()
    async with db_session() as db:
        db.add(MilestoneMeasurement(ladder_id=lid, scope="numbers", key="n"))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(MilestoneMeasurement(ladder_id=lid, scope="numbers", key="n"))
            await db.commit()


@pytest.mark.asyncio
async def test_one_ladder_per_project(isolated_db):
    await _ladder()
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id))).scalar_one()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(MilestoneLadder(project_id=pid, source_path="x"))
            await db.commit()
