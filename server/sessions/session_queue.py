"""Pure CRUD for the session queue (status='queued' sessions).

Ordering is (priority ASC, enqueued_at ASC). Mutations renumber priorities to
1..N in ONE transaction — the queue is human-scale, clarity beats cleverness.
No orchestrator imports: the orchestrator calls this module, never vice versa.
"""
from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import delete as sa_delete, select

from server.db import db_session
from server.db.models import SessionQueue

_ORDER = (SessionQueue.priority, SessionQueue.enqueued_at)


async def enqueue(session_id: str, params: dict) -> int:
    async with db_session() as db:
        rows = (await db.execute(select(SessionQueue.priority))).scalars().all()
        priority = (max(rows) + 1) if rows else 1
        db.add(SessionQueue(session_id=session_id, priority=priority, params=params))
        return priority


async def next_entry(exclude: Collection[str] = ()) -> SessionQueue | None:
    """Head of the queue, skipping ``exclude`` (entries the caller may not act on —
    the drain passes the ids owned by another fleet daemon)."""
    stmt = select(SessionQueue).order_by(*_ORDER).limit(1)
    if exclude:
        stmt = stmt.where(SessionQueue.session_id.notin_(list(exclude)))
    async with db_session() as db:
        return (await db.execute(stmt)).scalar_one_or_none()


async def remove(session_id: str) -> bool:
    async with db_session() as db:
        r = await db.execute(
            sa_delete(SessionQueue).where(SessionQueue.session_id == session_id))
        return bool(getattr(r, "rowcount", 0))


async def positions() -> dict[str, int]:
    async with db_session() as db:
        ids = (await db.execute(
            select(SessionQueue.session_id).order_by(*_ORDER))).scalars().all()
    return {sid: i + 1 for i, sid in enumerate(ids)}


async def move(session_id: str, op: str, position: int | None = None) -> bool:
    """Reorder one entry. op: up | down | top | position (1-based `position`)."""
    async with db_session() as db:
        rows = (await db.execute(
            select(SessionQueue).order_by(*_ORDER))).scalars().all()
        ids = [r.session_id for r in rows]
        if session_id not in ids:
            return False
        idx = ids.index(session_id)
        if op == "up":
            new = max(0, idx - 1)
        elif op == "down":
            new = min(len(ids) - 1, idx + 1)
        elif op == "top":
            new = 0
        elif op == "position" and position is not None:
            new = min(max(0, position - 1), len(ids) - 1)
        else:
            return False
        ids.insert(new, ids.pop(idx))
        by_id = {r.session_id: r for r in rows}
        for i, sid in enumerate(ids):
            by_id[sid].priority = i + 1
        return True
