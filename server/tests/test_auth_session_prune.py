"""Tests for AppSession pruning — expired/revoked rows are deleted after the grace window."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AppSession
from server.auth.middleware import _PRUNE_GRACE, prune_app_sessions


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_prune_removes_long_expired_rows():
    """Rows expired longer than _PRUNE_GRACE are deleted."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    stale_expiry = _now() - _PRUNE_GRACE - timedelta(hours=1)
    async with db_session() as db:
        s = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=stale_expiry - timedelta(hours=24),
            expires_at=stale_expiry,
            revoked_at=None,
        )
        db.add(s)
        await db.commit()

    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 1

    async with db_session() as db:
        rows = (await db.execute(select(AppSession))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_prune_removes_long_revoked_rows():
    """Rows revoked longer than _PRUNE_GRACE are deleted even if expires_at is in the future."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    old_revoked_at = _now() - _PRUNE_GRACE - timedelta(hours=1)
    async with db_session() as db:
        s = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=old_revoked_at,
            expires_at=_now() + timedelta(hours=1),  # still "live" by expiry
            revoked_at=old_revoked_at,
        )
        db.add(s)
        await db.commit()

    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 1


@pytest.mark.asyncio
async def test_prune_spares_recently_expired_rows():
    """Rows expired within _PRUNE_GRACE are kept (still useful for inspection)."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    recent_expiry = _now() - timedelta(hours=1)
    async with db_session() as db:
        s = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=recent_expiry - timedelta(hours=24),
            expires_at=recent_expiry,
            revoked_at=None,
        )
        db.add(s)
        await db.commit()

    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 0


@pytest.mark.asyncio
async def test_prune_spares_live_rows():
    """Active (non-expired, non-revoked) sessions are never pruned."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    now = _now()
    async with db_session() as db:
        s = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=now, expires_at=now + timedelta(hours=20), revoked_at=None,
        )
        db.add(s)
        sid = s.id
        await db.commit()

    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 0

    async with db_session() as db:
        row = (await db.execute(select(AppSession).where(AppSession.id == sid))).scalar_one()
    assert row.id == sid


@pytest.mark.asyncio
async def test_prune_returns_zero_on_empty_table():
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 0


@pytest.mark.asyncio
async def test_prune_mixed_batch():
    """Stale rows are removed; live rows survive."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    now = _now()
    stale_at = now - _PRUNE_GRACE - timedelta(hours=1)
    async with db_session() as db:
        stale = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=stale_at - timedelta(hours=24), expires_at=stale_at, revoked_at=None,
        )
        live = AppSession(
            id=str(uuid.uuid4()), identity="u@example.com",
            created_at=now, expires_at=now + timedelta(hours=20), revoked_at=None,
        )
        db.add(stale)
        db.add(live)
        await db.commit()
        stale_id, live_id = stale.id, live.id

    async with db_session() as db:
        deleted = await prune_app_sessions(db)
        await db.commit()
    assert deleted == 1

    async with db_session() as db:
        ids = {r.id for r in (await db.execute(select(AppSession))).scalars().all()}
    assert ids == {live_id}
    assert stale_id not in ids
