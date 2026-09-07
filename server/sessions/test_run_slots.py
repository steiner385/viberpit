"""Pure CRUD for the verification-phase test-run slot cap (#822).

One row per session that currently holds or is waiting for a "running the
full server-test suite" slot, keyed by host. Capacity is the count of
status="holding" rows for that host, checked against a caller-supplied cap
(server.config.Settings.test_slot_cap) — this module has no opinion on the
cap value itself. No orchestrator imports: callers call this module, never
vice versa (mirrors server/sessions/session_queue.py).

Every function here can raise (a normal DB error propagates from db_session).
Fail-open behavior — treating an error as "granted"/"released" so a session is
never blocked from running tests — is the CALLER's responsibility (see
server/api/test_slots.py), not this module's: a pure CRUD layer that silently
swallowed errors would make callers unable to tell a real failure from a
successful no-op.
"""
from __future__ import annotations

from sqlalchemy import func, select

from server.db import db_session
from server.db.models import TestRunSlot

HOLDING = "holding"
WAITING = "waiting"


async def _ahead(db, host: str, requested_at) -> int:
    return (await db.execute(
        select(func.count()).select_from(TestRunSlot)
        .where(TestRunSlot.host == host, TestRunSlot.status == WAITING,
               TestRunSlot.requested_at < requested_at)
    )).scalar_one()


async def acquire(session_id: str, host: str, *, cap: int) -> tuple[bool, int]:
    """Try to grant a slot; else queue. Returns (granted, ahead).

    ``ahead`` is the number of earlier waiters when queued (always 0 when
    granted). Idempotent: calling again for a session that already holds or
    is waiting just re-reads its current state instead of creating a second row.
    """
    async with db_session() as db:
        existing = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one_or_none()
        if existing is not None:
            if existing.status == HOLDING:
                return True, 0
            return False, await _ahead(db, existing.host, existing.requested_at)

        holding_count = (await db.execute(
            select(func.count()).select_from(TestRunSlot)
            .where(TestRunSlot.host == host, TestRunSlot.status == HOLDING)
        )).scalar_one()
        if holding_count < cap:
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
