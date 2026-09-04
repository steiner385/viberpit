"""DB-side primary-daemon lease — "exactly one primary" as a checked invariant (#852).

``CCSWITCH_DAEMON_ROLE`` is per-host env config: nothing structural stops two hosts
both being configured ``primary``, and two primaries double-drive every fleet-global
automation with no detection. This module enforces the invariant through a single
``daemon_leases`` row (``name='primary'``):

- ``acquire()`` — a primary daemon must win this before finishing ``start()``. It
  succeeds when the row is absent, already held by the SAME host (systemd guarantees
  one daemon per host, so a crash restart re-acquires instantly), or held by another
  host whose heartbeat has gone stale (fenced takeover). A fresh foreign lease means
  another primary is alive → the caller refuses to boot, loudly.
- ``renew()`` — the heartbeat. Conditional on (holder, fence), so a daemon whose
  lease was fenced over cannot resurrect it with a stale fence; a failed renew is the
  runtime split-brain signal.
- ``release()`` — clean shutdown hands the lease back so a planned failover never
  waits out the TTL. Same (holder, fence) condition. The row is marked immediately
  stale rather than deleted, so the fence stays monotonic across the lease's whole
  lifetime — a re-insert restarting at fence=1 could collide with a fence some
  stale writer still remembers.

Every mutation is a single conditional UPDATE/DELETE (rowcount-checked) or a
PK-guarded INSERT, so two racing acquirers serialize on the database — the loser's
WHERE clause no longer matches — with no dialect-specific locking (SQLite + Postgres,
same SQL, per the portable-SQL norm).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from server.db import db_session
from server.db.models import DaemonLease

log = logging.getLogger(__name__)

PRIMARY_LEASE_NAME = "primary"


@dataclass(frozen=True)
class LeaseState:
    """Read-only snapshot of the lease row (diagnostics / error messages)."""
    holder: str
    fence: int
    renewed_at: datetime
    age_s: float


async def current(name: str = PRIMARY_LEASE_NAME) -> LeaseState | None:
    """The lease row as-is, or None when nobody has ever held it."""
    async with db_session() as db:
        row = (await db.execute(
            sa_select(DaemonLease).where(DaemonLease.name == name))).scalars().first()
        if row is None:
            return None
        age = (datetime.utcnow() - row.renewed_at).total_seconds()
        return LeaseState(holder=row.holder, fence=row.fence,
                          renewed_at=row.renewed_at, age_s=age)


async def acquire(holder: str, *, ttl_s: float,
                  name: str = PRIMARY_LEASE_NAME) -> int | None:
    """Try to take the lease for *holder*. Returns the new fence, or None if a
    DIFFERENT host holds it with a fresh heartbeat (another primary is alive)."""
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(seconds=ttl_s)
    async with db_session() as db:
        # One conditional UPDATE covers both takeover paths: our own row (any age)
        # and a foreign row whose heartbeat lapsed. Two racing acquirers serialize
        # here — whoever commits first renews the row, so the loser's staleness
        # predicate stops matching and its rowcount is 0.
        result = await db.execute(
            sa_update(DaemonLease)
            .where(DaemonLease.name == name)
            .where((DaemonLease.holder == holder)
                   | (DaemonLease.renewed_at <= stale_cutoff))
            .values(holder=holder, acquired_at=now, renewed_at=now,
                    fence=DaemonLease.fence + 1)
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]  # CursorResult at runtime
            fence = (await db.execute(
                sa_select(DaemonLease.fence).where(DaemonLease.name == name)
            )).scalar_one()  # same transaction — reads our own uncommitted bump
            log.info("primary-lease: acquired by %r (fence=%d)", holder, fence)
            return fence
        # No row matched: either the lease has never existed, or a live foreign
        # holder has it. Distinguish by trying the PK-guarded INSERT — a racing
        # first-acquirer loses on the PK and lands in the held case.
        try:
            await db.execute(sa_insert(DaemonLease).values(
                name=name, holder=holder, fence=1, acquired_at=now, renewed_at=now))
        except Exception:
            await db.rollback()  # clear the failed statement; exit commit is a no-op
            return None
        log.info("primary-lease: acquired by %r (fence=1, first holder)", holder)
        return 1


async def renew(holder: str, fence: int, *, name: str = PRIMARY_LEASE_NAME) -> bool:
    """Heartbeat: refresh renewed_at iff we still hold the lease at our fence.
    False means the lease was taken over (fenced) — the split-brain signal."""
    async with db_session() as db:
        result = await db.execute(
            sa_update(DaemonLease)
            .where(DaemonLease.name == name,
                   DaemonLease.holder == holder,
                   DaemonLease.fence == fence)
            .values(renewed_at=datetime.utcnow())
        )
        return result.rowcount == 1  # type: ignore[attr-defined]  # CursorResult at runtime


async def release(holder: str, fence: int, *, name: str = PRIMARY_LEASE_NAME) -> None:
    """Hand the lease back on clean shutdown (no-op if it was fenced over).

    Marks the heartbeat epoch-stale instead of deleting the row: any acquirer's
    staleness predicate now matches immediately, while the fence keeps counting
    monotonically from where it was (a delete would restart the next holder at
    fence=1, colliding with fences stale writers may still remember)."""
    async with db_session() as db:
        result = await db.execute(
            sa_update(DaemonLease)
            .where(DaemonLease.name == name,
                   DaemonLease.holder == holder,
                   DaemonLease.fence == fence)
            .values(renewed_at=datetime(1970, 1, 1))
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]  # CursorResult at runtime
            log.info("primary-lease: released by %r (fence=%d)", holder, fence)
