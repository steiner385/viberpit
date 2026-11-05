"""Release-milestone tables: roundtrip and uniqueness constraints (0105)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from server.db import db_session
from server.db.models import (
    Milestone,
    MilestoneLadder,
    MilestoneMeasurement,
    MilestoneNote,
    Project,
)


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


@pytest.mark.asyncio
async def test_milestone_note_roundtrip(isolated_db):
    lid = await _ladder()
    at = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    async with db_session() as db:
        d = MilestoneNote(
            ladder_id=lid, milestone_key="household", kind="decision", ref="D4",
            text="Ship web first", url="https://example.com/d4", status="locked",
            created_by="owner", created_at=at, updated_at=at)
        a = MilestoneNote(
            ladder_id=lid, kind="ask", text="Pick a name", status="done",
            created_by="agent", created_at=at, updated_at=at,
            resolved_by="owner", resolved_at=at)
        db.add_all([d, a])
        await db.commit()
        did, aid = d.id, a.id
    async with db_session() as db:
        d = await db.get(MilestoneNote, did)
        a = await db.get(MilestoneNote, aid)
        assert d is not None and a is not None
        assert (d.ladder_id, d.milestone_key, d.kind, d.ref, d.text, d.url, d.status) == (
            lid, "household", "decision", "D4", "Ship web first",
            "https://example.com/d4", "locked")
        assert (d.created_by, d.resolved_by, d.resolved_at) == ("owner", None, None)
        assert d.created_at.replace(tzinfo=timezone.utc) == at
        assert d.updated_at.replace(tzinfo=timezone.utc) == at
        assert (a.milestone_key, a.ref, a.url) == (None, None, None)
        assert (a.kind, a.status, a.resolved_by) == ("ask", "done", "owner")
        assert a.resolved_at.replace(tzinfo=timezone.utc) == at
