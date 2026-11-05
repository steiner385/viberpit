"""``server/pm/milestone_asks.open_milestone_asks`` (spec 2026-11-05 M11): THE query for
open owner asks, grouped by project id, oldest first."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.db import db_session
from server.db.models import MilestoneLadder, MilestoneNote, Project
from server.pm.milestone_asks import open_milestone_asks


async def _ladder(slug: str) -> tuple[str, str]:
    async with db_session() as db:
        p = Project(slug=slug, name=slug)
        db.add(p)
        await db.flush()
        lad = MilestoneLadder(project_id=p.id, definition=None, title=slug)
        db.add(lad)
        await db.flush()
        return p.id, lad.id


async def _note(ladder_id: str, text: str, *, kind: str = "ask", status: str = "open",
                age_s: int = 0, milestone_key: str | None = None) -> str:
    async with db_session() as db:
        n = MilestoneNote(ladder_id=ladder_id, kind=kind, text=text, status=status,
                          milestone_key=milestone_key,
                          created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s))
        db.add(n)
        await db.flush()
        return n.id


@pytest.mark.asyncio
async def test_open_asks_grouped_by_project_oldest_first(isolated_db):
    pa, la = await _ladder("a")
    pb, lb = await _ladder("b")
    await _note(la, "newer", age_s=10, milestone_key="household")
    await _note(la, "older", age_s=100)        # ladder-wide asks count too
    await _note(lb, "b-only", age_s=5, milestone_key="removed-rung")
    async with db_session() as db:
        every = await open_milestone_asks(db)
        only_a = await open_milestone_asks(db, [pa])
        none = await open_milestone_asks(db, [])
    assert {k: [n.text for n in v] for k, v in every.items()} == {
        pa: ["older", "newer"], pb: ["b-only"]}
    assert list(only_a) == [pa]
    assert none == {}


@pytest.mark.asyncio
async def test_done_and_dropped_asks_excluded(isolated_db):
    pa, la = await _ladder("a")
    await _note(la, "done", status="done")
    await _note(la, "dropped", status="dropped")
    await _note(la, "a decision", kind="decision", status="proposed")
    keep = await _note(la, "still open")
    async with db_session() as db:
        got = await open_milestone_asks(db)
    assert [n.id for n in got[pa]] == [keep]
