"""Milestone note vocabulary and THE open-asks query (spec 2026-11-05 M11).

An open owner ask is a ``MilestoneNote`` with ``kind == "ask"`` and status ``open``,
on any rung of the ladder or ladder-wide, including a removed rung (the owner
resolves it). :func:`open_milestone_asks` is the only query for them: the map badge
(``tree.needs_you_total``, via ``pm_tree.product_nodes``), the hygiene sampler and the
``roadmap_needs_you`` tool all read it. :func:`is_open_ask` is the same rule over an
already-loaded row (the milestones view).

The vocabulary lives here, a leaf module, so the view (``milestone_view``) and the write
rules (``milestone_actions``, which imports the view) share it without an import cycle.
"""
from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import MilestoneLadder, MilestoneNote

NOTE_KINDS: tuple[str, ...] = ("decision", "ask")
DECISION_STATUSES: tuple[str, ...] = ("locked", "proposed", "superseded")
ASK_STATUSES: tuple[str, ...] = ("open", "done", "dropped")
ASK_KIND = "ask"
OPEN_ASK_STATUS = "open"


def is_open_ask(note: Any) -> bool:
    return note.kind == ASK_KIND and note.status == OPEN_ASK_STATUS


async def open_milestone_asks(
    db: AsyncSession, project_ids: Collection[str] | None = None,
) -> dict[str, list[MilestoneNote]]:
    """Open asks keyed by project id, oldest first, in one query. *project_ids*
    ``None`` means every project; an empty collection means none."""
    if project_ids is not None and not project_ids:
        return {}
    stmt = (select(MilestoneLadder.project_id, MilestoneNote)
            .join(MilestoneLadder, MilestoneLadder.id == MilestoneNote.ladder_id)
            .where(MilestoneNote.kind == ASK_KIND, MilestoneNote.status == OPEN_ASK_STATUS)
            .order_by(MilestoneNote.created_at, MilestoneNote.id))
    if project_ids is not None:
        stmt = stmt.where(MilestoneLadder.project_id.in_(list(project_ids)))
    out: dict[str, list[MilestoneNote]] = {}
    for pid, note in (await db.execute(stmt)).all():
        out.setdefault(pid, []).append(note)
    return out
