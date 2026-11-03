"""Per-viewer session read state (D-10), served by the edge straight from the DB.

A session is **unread** for a viewer iff a ``session_views`` row exists for that
(viewer, session) AND the session's ``last_turn_at`` is later than ``viewed_at``. With
no row it is read, so sessions nobody has opened (every pre-existing and
automation-only session) never turn bold. ``last_turn_at`` is the persisted "a turn
handed output back" marker; the daemon's event seq resets on restart, so it can't be
the comparison point.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select

from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import Session as DbSession, SessionView

# How far before the last turn "mark as unread" puts viewed_at: the smallest step a
# naive DateTime keeps on both dialects, so that one turn reads as new.
_UNREAD_STEP = timedelta(microseconds=1)


class UnknownSession(LookupError):
    """No sessions row with that id."""


async def unread_ids(identity: str) -> set[str]:
    """Ids of every session unread for ``identity`` — ONE query, whatever the list size."""
    async with db_session() as db:
        rows = await db.execute(
            select(SessionView.session_id)
            .join(DbSession, DbSession.id == SessionView.session_id)
            .where(SessionView.identity == identity,
                   DbSession.last_turn_at.is_not(None),
                   DbSession.last_turn_at > SessionView.viewed_at))
        return set(rows.scalars())


async def _upsert(db, identity: str, session_id: str, viewed_at) -> None:
    stmt = dialect_insert(SessionView).values(
        identity=identity, session_id=session_id, viewed_at=viewed_at)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["identity", "session_id"],
        set_={"viewed_at": stmt.excluded.viewed_at}))


async def _last_turn_at(db, session_id: str):
    row = (await db.execute(
        select(DbSession.id, DbSession.last_turn_at).where(DbSession.id == session_id)
    )).first()
    if row is None:
        raise UnknownSession(session_id)
    return row.last_turn_at


async def mark_viewed(identity: str, session_id: str) -> None:
    """``viewed_at`` = max(the DB clock now, the session's ``last_turn_at``).
    Raises :class:`UnknownSession`.

    ``last_turn_at`` is stamped by whichever host ran the turn; a worker whose clock
    runs ahead of the DB's would otherwise leave a session the viewer has just read
    bold until real time caught up. Taking the max means "viewed" always covers the
    last turn, and only a later turn reads as new."""
    async with db_session() as db:
        # One SELECT, one transaction: the row's last turn and the DB clock. DB clock
        # rule: db_utc_now() comes back as a concrete naive-UTC datetime on both
        # dialects, so the max is a plain Python comparison — no dialect GREATEST/MAX.
        row = (await db.execute(
            select(DbSession.last_turn_at, db_utc_now()).where(DbSession.id == session_id)
        )).first()
        if row is None:
            raise UnknownSession(session_id)
        last_turn_at, now = row
        viewed_at = now if last_turn_at is None else max(now, last_turn_at)
        await _upsert(db, identity, session_id, viewed_at)


async def mark_unread(identity: str, session_id: str) -> None:
    """Make the last turn read as new; with no turn yet, drop the row (= read).
    Raises :class:`UnknownSession`."""
    async with db_session() as db:
        last_turn_at = await _last_turn_at(db, session_id)
        if last_turn_at is None:
            await db.execute(delete(SessionView).where(
                SessionView.identity == identity, SessionView.session_id == session_id))
            return
        await _upsert(db, identity, session_id, last_turn_at - _UNREAD_STEP)


async def delete_for_session(db, session_id: str) -> None:
    """Drop every viewer's row for a session being permanently deleted. The FK is ON
    DELETE CASCADE, but SQLite does not enforce FKs by default, so delete explicitly."""
    await db.execute(delete(SessionView).where(SessionView.session_id == session_id))
