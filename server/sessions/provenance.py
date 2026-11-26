"""A session's durable lifecycle timeline (spec 002 FR-301, #947).

The ONE vocabulary and writer for ``session_events`` rows. Each kind answers a
question the user would otherwise have to dig out of the daemon logs ("why did
this queued session end?", "who archived it?"). ``AuditEvent`` was evaluated for
this and rejected: it is the security log, keyed by a NOT NULL ``identity``.

Recording is provenance, never control flow: :func:`record` swallows and logs
every failure, so a timeline write can never break the turn, spawn or archive
path that called it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import insert, select

from server.db import db_session
from server.db.clock import naive_utc_now
from server.db.models import SessionEvent
from server.time_iso import iso_utc

log = logging.getLogger(__name__)

#: A queued session failed to start. detail: ``spawn_failure.SpawnFailure.to_detail()``
#: — ``{reason, transient, attempt, message}``; ``requeued`` is true when the drain
#: put it back for its one retry instead of ending it.
SPAWN_FAILED = "spawn_failed"
#: The session was archived by automation, not a person. detail: ``{by, reason}``,
#: where ``by`` is the ``sessions.archived_by`` provenance value.
AUTO_ARCHIVED = "auto_archived"
#: The queue drain's retry of a failed start will run on a different account
#: (FR-303/FR-304). detail: ``{from, reason, attempt}`` — the account the failed
#: attempt ran on, the ``spawn_failed`` reason that moved it, that attempt's number.
ROTATED_ACCOUNT = "rotated_account"

KINDS = frozenset({SPAWN_FAILED, AUTO_ARCHIVED, ROTATED_ACCOUNT})

#: Newest-first page size for :func:`list_events` when the caller gives none.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


async def record(session_id: str, kind: str, detail: dict[str, Any] | None = None) -> None:
    """Append one timeline entry. Never raises (see module docstring)."""
    if kind not in KINDS:
        log.error("provenance.unknown_kind session=%s kind=%s", session_id, kind)
        return
    try:
        async with db_session() as db:
            await db.execute(insert(SessionEvent).values(
                session_id=session_id, kind=kind, ts=naive_utc_now(), detail=detail))
    except Exception:
        log.warning("provenance.record_failed session=%s kind=%s", session_id, kind,
                    exc_info=True)


async def list_events(session_id: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """The session's timeline, newest first, in wire shape."""
    limit = max(1, min(limit, MAX_LIMIT))
    async with db_session() as db:
        rows = (await db.execute(
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .order_by(SessionEvent.ts.desc(), SessionEvent.id.desc())
            .limit(limit))).scalars().all()
    return [{"id": r.id, "kind": r.kind, "ts": iso_utc(r.ts), "detail": r.detail or {}}
            for r in rows]
