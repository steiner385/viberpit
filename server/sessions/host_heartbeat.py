"""DB-side per-host daemon heartbeat — fenced liveness primitives (spec 002 FR-101).

Each host daemon owns exactly one row in ``host_heartbeats`` (keyed on host name).
``fence`` is a monotonic counter that increments whenever the primary declares the
host dead. A host that has been fenced over cannot retroactively mark itself alive
by calling ``beat()`` with a stale fence — the conditional UPDATE's WHERE clause
rejects it (FR-202 trigger).

- ``register(host)`` — idempotent insert: tries a PK-guarded INSERT; on PK collision
  rolls back and returns the EXISTING fence so a restarted host adopts the new epoch
  transparently (a fenced-while-down host simply re-registers under the bumped fence).
- ``beat(host, fence)`` — conditional ``UPDATE … WHERE host=? AND fence=?`` setting
  ``beat_at=utcnow()``. Returns ``False`` when the fence no longer matches (fenced out).
- ``declare_dead(host, *, dead_after_s, observed_fence)`` — compare-and-swap bump:
  ``UPDATE … WHERE host=? AND fence=observed_fence AND beat_at<=cutoff`` sets
  ``fence=fence+1``. Exactly one winner under races; returns the new fence or ``None``
  (row missing, beat too fresh, or already bumped by another caller).
- ``snapshot()`` — read-only: returns a ``HeartbeatState`` dict keyed by host.

Every mutation is a single conditional statement (rowcount-checked) or a PK-guarded
INSERT — no dialect-specific locking; SQLite + Postgres, same SQL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from server.db import db_session
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


async def register(host: str, *, epoch: str | None = None) -> int:
    """Idempotent insert — returns the current fence (1 on first insert; the EXISTING
    fence if the row survived from a prior epoch so the caller adopts it transparently)."""
    async with db_session() as db:
        try:
            await db.execute(sa_insert(HostHeartbeat).values(
                host=host, fence=1, beat_at=datetime.utcnow(), epoch=epoch))
        except Exception:
            await db.rollback()
            row = (await db.execute(
                sa_select(HostHeartbeat.fence).where(HostHeartbeat.host == host)
            )).scalar_one()
            log.info("host-heartbeat: %r already registered (fence=%d)", host, row)
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
            .values(beat_at=datetime.utcnow())
        )
        return result.rowcount == 1  # type: ignore[attr-defined]  # CursorResult at runtime


async def declare_dead(host: str, *, dead_after_s: float, observed_fence: int) -> int | None:
    """Compare-and-swap fence bump: declares *host* dead if its beat is stale AND the
    fence still matches *observed_fence* (exactly-one-winner under races).

    Returns the new fence on success, or None when the row is missing, the beat is
    too fresh, or another caller already bumped the fence."""
    cutoff = datetime.utcnow() - timedelta(seconds=dead_after_s)
    async with db_session() as db:
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


async def snapshot() -> dict[str, HeartbeatState]:
    """Read-only snapshot of all host_heartbeats rows, keyed by host."""
    now = datetime.utcnow()
    async with db_session() as db:
        rows = (await db.execute(sa_select(HostHeartbeat))).scalars().all()
    return {
        row.host: HeartbeatState(
            host=row.host,
            beat_at=row.beat_at,
            fence=row.fence,
            epoch=row.epoch,
            age_s=(now - row.beat_at).total_seconds(),
        )
        for row in rows
    }
