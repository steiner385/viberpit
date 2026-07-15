"""Async read/write/prune over merge_queue_observations (Approach A: raw snapshots).

Each poll writes one row per queued PR. The 24h window is read back and grouped into
Observation objects for the pure derivation in merge_train.py.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from server.db import db_session
from server.db.models import MergeQueueObservation
from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation


async def record(repo: str, captured_at: int, entries: tuple[QueueEntry, ...]) -> None:
    async with db_session() as s:
        for e in entries:
            s.add(MergeQueueObservation(
                repo=repo, captured_at=captured_at, pr_number=e.pr_number,
                position=e.position, state=e.state, enqueued_at=e.enqueued_at,
                batch_head_oid=e.batch_head_oid, group_checks=e.group_checks,
                checks_total=e.checks_total, checks_done=e.checks_done,
                eta_seconds=e.eta_seconds))
        if not entries:
            # Data-of-absence sentinel (pr_number NULL): an EMPTY queue must
            # still produce an observation, or window()'s newest snapshot stays
            # the last busy one and the dashboard replays a merged train for up
            # to 24h ("#11778 building 94%" three hours after it merged).
            s.add(MergeQueueObservation(
                repo=repo, captured_at=captured_at, pr_number=None))
        await s.commit()


def _entry(row: MergeQueueObservation) -> QueueEntry:
    assert row.pr_number is not None  # callers skip empty-queue sentinel rows
    return QueueEntry(
        pr_number=row.pr_number, position=row.position, state=row.state or "",
        enqueued_at=row.enqueued_at, batch_head_oid=row.batch_head_oid,
        group_checks=row.group_checks, checks_total=row.checks_total,
        checks_done=row.checks_done, eta_seconds=row.eta_seconds)


async def window(repo: str, since_epoch: int) -> list[Observation]:
    async with db_session() as s:
        rows = (await s.execute(
            select(MergeQueueObservation)
            .where(MergeQueueObservation.repo == repo,
                   MergeQueueObservation.captured_at >= since_epoch)
            .order_by(MergeQueueObservation.captured_at)
        )).scalars().all()
    grouped: dict[int, list[QueueEntry]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.captured_at, [])
        # pr_number NULL is the empty-queue sentinel: it pins the observation's
        # captured_at but contributes no entry.
        if row.pr_number is not None:
            bucket.append(_entry(row))
    return [Observation(captured_at=t, entries=tuple(es)) for t, es in sorted(grouped.items())]


async def prune(older_than_epoch: int) -> int:
    async with db_session() as s:
        result = await s.execute(
            delete(MergeQueueObservation)
            .where(MergeQueueObservation.captured_at < older_than_epoch))
        await s.commit()
        return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult at runtime
