"""DB-side per-host daemon heartbeat — fenced liveness primitives (spec 002 FR-101).

Each host daemon owns exactly one row in ``host_heartbeats`` (keyed on host name).
``fence`` is a monotonic counter that increments whenever the primary declares the
host dead. A host that has been fenced over cannot retroactively mark itself alive
by calling ``beat()`` with a stale fence — the conditional UPDATE's WHERE clause
rejects it (FR-202 trigger).

- ``register(host)`` — idempotent insert: tries a PK-guarded INSERT; on PK collision
  rolls back and returns the EXISTING fence so a restarted host adopts the new epoch
  transparently (a fenced-while-down host simply re-registers under the bumped fence).
  The collision path also refreshes ``epoch`` so the surviving row reflects the NEW
  incarnation rather than the dead process's — the one job FR-101 gives ``epoch``.
- ``beat(host, fence)`` — conditional ``UPDATE … WHERE host=? AND fence=?`` setting
  ``beat_at`` to the DB clock. Returns ``False`` when the fence no longer matches.
- ``declare_dead(host, *, dead_after_s, observed_fence)`` — compare-and-swap bump:
  ``UPDATE … WHERE host=? AND fence=observed_fence AND beat_at<=cutoff`` sets
  ``fence=fence+1``. Exactly one winner under races; returns the new fence or ``None``
  (row missing, beat too fresh, or already bumped by another caller).
- ``snapshot()`` — read-only: returns a ``HostHeartbeats`` (the DB clock plus a
  ``HeartbeatState`` dict keyed by host).

Every mutation is a single conditional statement (rowcount-checked) or a PK-guarded
INSERT — no dialect-specific locking; SQLite + Postgres, same SQL.

**All timestamps are the DATABASE's clock, never the writer's or reader's** (spec 002
FR-102): ``beat_at`` is written with the DB clock (``db_utc_now()`` — naive UTC on
every dialect) and ``snapshot()`` / ``declare_dead`` compare it against the same clock
read from the same DB. A
daemon host whose local clock lags the edge by minutes therefore can never be falsely
demoted — the skew that used to read a healthy worker as ``unreachable`` is gone by
construction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import HostHeartbeat

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeartbeatState:
    """Read-only snapshot of a host_heartbeats row (diagnostics / liveness input)."""
    host: str
    beat_at: datetime
    fence: int
    epoch: str | None
    age_s: float


@dataclass(frozen=True)
class HostHeartbeats:
    """All host_heartbeats rows plus the DB clock they were read at.

    Thresholds are evaluated against ``now`` (the DB's clock, not the edge's) so a
    host whose local clock differs from the reader is never falsely demoted — this
    is what makes FR-102's "evaluated against DB time" true at the read side."""
    now: datetime
    hosts: dict[str, HeartbeatState]


async def _db_now(db) -> datetime:
    """The database server's current timestamp — the single clock all liveness math
    is measured against (spec 002 FR-102) — as naive UTC, the same shape as the
    ``beat_at`` column it is subtracted from. NOT a bare ``func.current_timestamp()``:
    on Postgres that is a ``timestamptz`` and comes back AWARE (see server/db/clock.py)."""
    return (await db.execute(sa_select(db_utc_now()))).scalar_one()


async def register(host: str, *, epoch: str | None = None) -> int:
    """Idempotent insert — returns the current fence (1 on first insert; the EXISTING
    fence if the row survived from a prior epoch so the caller adopts it transparently).

    On the collision path the surviving row's ``epoch`` is refreshed to *epoch* (when
    given) so a restarted daemon that adopts the fence is distinguishable from the dead
    incarnation it replaced (FR-101) — otherwise the row would carry the first process's
    epoch forever and Phase 4 fenced-adoption would read a permanently stale value."""
    async with db_session() as db:
        try:
            await db.execute(sa_insert(HostHeartbeat).values(
                host=host, fence=1, beat_at=db_utc_now(), epoch=epoch))
        except Exception:
            await db.rollback()
            if epoch is not None:
                await db.execute(
                    sa_update(HostHeartbeat)
                    .where(HostHeartbeat.host == host)
                    .values(epoch=epoch)
                )
            row = (await db.execute(
                sa_select(HostHeartbeat.fence).where(HostHeartbeat.host == host)
            )).scalar_one()
            log.info("host-heartbeat: %r already registered (fence=%d, epoch refreshed=%s)",
                     host, row, epoch is not None)
            return row
        log.info("host-heartbeat: %r registered (fence=1)", host)
        return 1


async def beat(host: str, fence: int) -> bool:
    """Heartbeat: refresh beat_at iff the host still holds the current fence.
    False means the fence was bumped (host was declared dead — FR-202 trigger)."""
    async with db_session() as db:
        result = await db.execute(
            sa_update(HostHeartbeat)
            .where(HostHeartbeat.host == host,
                   HostHeartbeat.fence == fence)
            .values(beat_at=db_utc_now())
        )
        return result.rowcount == 1  # type: ignore[attr-defined]  # CursorResult at runtime


async def declare_dead(host: str, *, dead_after_s: float, observed_fence: int) -> int | None:
    """Compare-and-swap fence bump: declares *host* dead if its beat is stale AND the
    fence still matches *observed_fence* (exactly-one-winner under races).

    Returns the new fence on success, or None when the row is missing, the beat is
    too fresh, or another caller already bumped the fence. The staleness cutoff is
    derived from the DB clock so it is measured against the same clock ``beat_at`` was
    written with (FR-102)."""
    async with db_session() as db:
        cutoff = (await _db_now(db)) - timedelta(seconds=dead_after_s)
        result = await db.execute(
            sa_update(HostHeartbeat)
            .where(HostHeartbeat.host == host,
                   HostHeartbeat.fence == observed_fence,
                   HostHeartbeat.beat_at <= cutoff)
            .values(fence=HostHeartbeat.fence + 1)
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]  # CursorResult at runtime
            new_fence = (await db.execute(
                sa_select(HostHeartbeat.fence).where(HostHeartbeat.host == host)
            )).scalar_one()
            log.info("host-heartbeat: %r declared dead (fence bumped to %d)", host, new_fence)
            return new_fence
        return None


async def snapshot() -> HostHeartbeats:
    """Read-only snapshot of all host_heartbeats rows, keyed by host, plus the DB
    clock the rows were read at (so callers evaluate thresholds against DB time)."""
    async with db_session() as db:
        now = await _db_now(db)
        rows = (await db.execute(sa_select(HostHeartbeat))).scalars().all()
    return HostHeartbeats(
        now=now,
        hosts={
            row.host: HeartbeatState(
                host=row.host,
                beat_at=row.beat_at,
                fence=row.fence,
                epoch=row.epoch,
                age_s=(now - row.beat_at).total_seconds(),
            )
            for row in rows
        },
    )
