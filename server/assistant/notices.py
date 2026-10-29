"""Durable assistant watch notices (#3183).

A fired watch writes an ``assistant_notices`` row FIRST; live drawer delivery is only
a shortcut on top. Unread rows are replayed when the drawer attaches and counted by
the nav badge, so a notice survives a closed drawer, a daemon restart, and a worker
host that cannot see the primary's assistant thread. Plain DB functions with no
orchestrator reference, so any daemon (primary or worker) and the edge share them.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from server.audit import record_audit
from server.db import db_session
from server.db.models import AssistantNotice

# A replay returns at most this many unread rows (newest) — a never-opened drawer
# must not dump an unbounded backlog into the transcript.
_UNREAD_LIMIT = 50


def notice_wire(row: AssistantNotice) -> dict:
    return {
        "id": row.id, "kind": row.kind, "title": row.title, "body": row.body,
        "url": row.url, "watchId": row.watch_id,
        "createdAt": row.created_at.isoformat() + "Z",
        "read": row.read_at is not None,
    }


async def record_notice(identity: str, notice: dict) -> str:
    """Persist one fired notice plus its audit row; return the notice id."""
    async with db_session() as db:
        row = AssistantNotice(
            identity=identity, kind=notice.get("kind", ""), title=notice.get("title", ""),
            body=notice.get("body", ""), url=notice.get("url"), watch_id=notice.get("watchId"),
        )
        db.add(row)
        await db.flush()
        await record_audit(db, "assistant.notice.deliver", identity,
                           {"noticeId": row.id, "kind": row.kind, "watchId": row.watch_id})
        await db.commit()
        return row.id


async def claim_unread(identity: str) -> list[dict]:
    """Return *identity*'s unread notices, oldest first, marking them read."""
    async with db_session() as db:
        rows = (await db.execute(
            select(AssistantNotice)
            .where(AssistantNotice.identity == identity, AssistantNotice.read_at.is_(None))
            .order_by(AssistantNotice.created_at.desc()).limit(_UNREAD_LIMIT)
        )).scalars().all()
        if not rows:
            return []
        out = [notice_wire(r) for r in reversed(rows)]
        # Everything unread is consumed, including rows past the limit: they were
        # shown as "N more" nowhere, and leaving them would pin the badge forever.
        await db.execute(update(AssistantNotice)
                         .where(AssistantNotice.identity == identity,
                                AssistantNotice.read_at.is_(None))
                         .values(read_at=datetime.utcnow()))
        await db.commit()
        return out


async def mark_read(identity: str, ids: list[str] | None = None) -> None:
    """Mark *ids* (or every unread notice when None) read for *identity*."""
    async with db_session() as db:
        stmt = (update(AssistantNotice)
                .where(AssistantNotice.identity == identity,
                       AssistantNotice.read_at.is_(None))
                .values(read_at=datetime.utcnow()))
        if ids is not None:
            stmt = stmt.where(AssistantNotice.id.in_(ids))
        await db.execute(stmt)
        await db.commit()


async def unread_count(identity: str) -> int:
    async with db_session() as db:
        return (await db.execute(
            select(func.count()).select_from(AssistantNotice)
            .where(AssistantNotice.identity == identity, AssistantNotice.read_at.is_(None))
        )).scalar_one()
