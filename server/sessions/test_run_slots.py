"""Pure CRUD for the verification-phase test-run slot cap (#822).

One row per session that currently holds or is waiting for a "running the
full server-test suite" slot, keyed by host. Capacity is the count of
status="holding" rows for that host, checked against a caller-supplied cap
(server.config.Settings.test_slot_cap) — this module has no opinion on the
cap value itself. No orchestrator imports: callers call this module, never
vice versa (mirrors server/sessions/session_queue.py).

``session_id`` is an opaque holder key chosen by the caller (a real
``sessions.id``, or a shell PID for the documented curl-based recipe in
.agents/instructions/core.md) — NOT a foreign key into the ``sessions``
table. The two identifier spaces are allowed to diverge on purpose: this
module never joins against ``sessions``, and requiring a real session row
made the cap silently inert for any holder that wasn't one (#822 final
review finding — the FK was tried and removed once that was shown).

Every function here can raise (a normal DB error propagates from db_session).
Fail-open behavior — treating an error as "granted"/"released" so a session is
never blocked from running tests — is the CALLER's responsibility (see
server/api/test_slots.py), not this module's: a pure CRUD layer that silently
swallowed errors would make callers unable to tell a real failure from a
successful no-op.

Stale holders are reaped (not merely queued around) so a session that dies
before it can release — a kill signal, a host reboot, a daemon restart — can't
wedge the host's cap forever: at cap=1 (the default) that would otherwise
deadlock every later session on the host, violating the "must never
deadlock" requirement this whole feature exists to satisfy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, func, select

from server.db import db_session
from server.db.models import TestRunSlot

HOLDING = "holding"
WAITING = "waiting"

# Longer than any real full-suite run (CI's serial run is ~25 min) but short
# enough that a wedged host self-heals within roughly one polling cycle's
# worth of patience. A HOLDING row older than this was abandoned by a session
# that died without releasing — treat it as gone.
_STALE_HOLD_S = 1800


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _ahead(db, host: str, requested_at) -> int:
    return (await db.execute(
        select(func.count()).select_from(TestRunSlot)
        .where(TestRunSlot.host == host, TestRunSlot.status == WAITING,
               TestRunSlot.requested_at < requested_at)
    )).scalar_one()


async def _reap_stale_holders(db, host: str) -> None:
    """Delete HOLDING rows for ``host`` older than ``_STALE_HOLD_S`` — see
    module docstring. Call this before any capacity check so acquire() always
    sees a cap that reflects only live holders."""
    cutoff = _utcnow() - timedelta(seconds=_STALE_HOLD_S)
    await db.execute(
        sa_delete(TestRunSlot).where(
            TestRunSlot.host == host, TestRunSlot.status == HOLDING,
            TestRunSlot.requested_at < cutoff))


async def _holding_count(db, host: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(TestRunSlot)
        .where(TestRunSlot.host == host, TestRunSlot.status == HOLDING)
    )).scalar_one()


async def acquire(session_id: str, host: str, *, cap: int) -> tuple[bool, int]:
    """Try to grant a slot; else queue. Returns (granted, ahead).

    ``ahead`` is the number of earlier waiters when queued (always 0 when
    granted). Idempotent: calling again for a session that already holds or
    is waiting just re-reads its current state, self-promoting a waiter to
    holding if a slot has freed up since it last polled (via this call's own
    stale-holder reap, or another session's release()) — instead of creating
    a second row or waiting forever behind a slot nothing will ever free.
    """
    async with db_session() as db:
        await _reap_stale_holders(db, host)

        existing = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if existing is not None:
            if existing.status == HOLDING:
                return True, 0
            if await _holding_count(db, existing.host) < cap:
                existing.status = HOLDING
                return True, 0
            return False, await _ahead(db, existing.host, existing.requested_at)

        if await _holding_count(db, host) < cap:
            db.add(TestRunSlot(session_id=session_id, host=host, status=HOLDING))
            return True, 0

        row = TestRunSlot(session_id=session_id, host=host, status=WAITING)
        db.add(row)
        await db.flush()
        return False, await _ahead(db, host, row.requested_at)


async def release(session_id: str) -> None:
    """Drop this session's row. If it held a slot, promote the oldest waiter
    for its host (if any) from 'waiting' to 'holding'. No-op if the session
    holds no row (already released, or never acquired)."""
    async with db_session() as db:
        row = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if row is None:
            return
        was_holding = row.status == HOLDING
        host = row.host
        await db.delete(row)
        await db.flush()
        if not was_holding:
            return
        next_waiter = (await db.execute(
            select(TestRunSlot)
            .where(TestRunSlot.host == host, TestRunSlot.status == WAITING)
            .order_by(TestRunSlot.requested_at)
            .limit(1)
        )).scalar_one_or_none()
        if next_waiter is not None:
            next_waiter.status = HOLDING


async def status(session_id: str, host: str) -> tuple[str, int]:
    """Current state for this session: ("holding", 0) | ("waiting", ahead) |
    ("none", 0) — the last means no row (never acquired, or already released)."""
    async with db_session() as db:
        row = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if row is None:
            return "none", 0
        if row.status == HOLDING:
            return "holding", 0
        return "waiting", await _ahead(db, row.host, row.requested_at)
