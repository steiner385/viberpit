"""Asking pm-sync for a forced pass over a product's repos.

One definition of "request a sync", shared by the two callers that need it (issue
#985): the ``POST /api/pm/projects/{slug}/sync`` handler and the reconciler's
``request_sync`` seam, which asks both BEFORE writing (a stale mirror is refused,
not written through) and AFTER (so our own writes are re-ingested and
echo-suppressed rather than read back as human edits).

The request is a watermark on every ``ProjectRepo`` row, not a queue entry: pm-sync
compares ``sync_requested_at`` against its own last pass, so re-stamping an
already-pending request is idempotent and two callers racing cannot double-sync.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from server.db.models import ProjectRepo


async def request_project_sync(db, project_id: str, now: datetime) -> int:
    """Stamp every repo of *project_id* as sync-requested at *now*; returns the count.

    Takes an ``AsyncSession`` and does NOT commit — the caller owns the transaction,
    so an API handler can audit in the same unit of work.
    """
    rows = (await db.execute(select(ProjectRepo).where(
        ProjectRepo.project_id == project_id))).scalars().all()
    for row in rows:
        row.sync_requested_at = now
    return len(rows)
