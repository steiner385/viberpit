"""The milestone-ladder DB seams for the pm-sync pass — the apply half of
``server/pm/milestone_index.py``'s planner (same split as
``persona_sync_seams.py``). Timestamps are aware Python UTC (``_utcnow``): every
milestone column is ``timestamptz``, and the naive ``db_utc_now()`` bound to one is
read by asyncpg as process-local time (hours off on an EDT host)."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Milestone, MilestoneLadder, _utcnow

from .milestone_index import LadderRowsView, LadderSyncPlan, MilestoneRowView


async def load_ladder_rows(project_id: str) -> LadderRowsView:
    async with db_session() as db:
        ladder = (await db.execute(
            select(MilestoneLadder).where(MilestoneLadder.project_id == project_id)
        )).scalar_one_or_none()
        if ladder is None:
            return LadderRowsView(exists=False, milestones=())
        rows = (await db.execute(
            select(Milestone).where(Milestone.ladder_id == ladder.id).order_by(Milestone.ordinal)
        )).scalars().all()
    return LadderRowsView(exists=True, milestones=tuple(
        MilestoneRowView(key=m.key, state=m.state, removed=m.removed_at is not None)
        for m in rows))


async def apply_ladder_plan(project_id: str, plan: LadderSyncPlan) -> None:
    if plan.is_noop:
        return
    async with db_session() as db:
        now = _utcnow()
        ladder = (await db.execute(
            select(MilestoneLadder).where(MilestoneLadder.project_id == project_id)
        )).scalar_one_or_none()
        if ladder is None:
            if not plan.create_ladder:
                return  # the ladder vanished since the plan was made; next pass re-plans
            ladder = MilestoneLadder(project_id=project_id)
            db.add(ladder)
            await db.flush()
        for column, value in plan.ladder_fields.items():
            setattr(ladder, column, value)
        ladder.synced_at = now

        by_key = {m.key: m for m in (await db.execute(
            select(Milestone).where(Milestone.ladder_id == ladder.id)
        )).scalars().all()}
        for up in plan.upserts:
            row = by_key.get(up["key"])
            if row is None:
                row = Milestone(ladder_id=ladder.id, key=up["key"])
                db.add(row)
                by_key[row.key] = row
            for column, value in up.items():
                if column != "key":
                    setattr(row, column, value)
        for key in plan.remove_keys:
            if key in by_key:
                by_key[key].removed_at = now
        for key in plan.revive_keys:
            if key in by_key:
                by_key[key].removed_at = None
        if plan.set_current is not None and plan.set_current in by_key:
            current = by_key[plan.set_current]
            current.state = "current"
            current.entered_at = now
        await db.flush()
