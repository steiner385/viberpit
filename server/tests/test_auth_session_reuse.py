import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AppSession


@pytest.mark.asyncio
async def test_cookieless_requests_reuse_one_session():
    """Ten cookie-less requests for one identity must not mint ten rows."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    class _Req:
        cookies: dict = {}

    for _ in range(10):
        async with db_session() as db:
            await _resolve_or_create_app_session(_Req(), db, "user@example.com")

    async with db_session() as db:
        rows = (await db.execute(select(AppSession))).scalars().all()
    assert len(rows) == 1, f"expected reuse, minted {len(rows)} rows"


@pytest.mark.asyncio
async def test_distinct_identities_do_not_share_a_session():
    """Reuse is scoped to the identity — never across principals."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    class _Req:
        cookies: dict = {}

    async with db_session() as db:
        a = await _resolve_or_create_app_session(_Req(), db, "a@example.com")
        b = await _resolve_or_create_app_session(_Req(), db, "b@example.com")
    assert a.id != b.id
    assert (a.identity, b.identity) == ("a@example.com", "b@example.com")


class _Req:
    cookies: dict = {}


@pytest.mark.asyncio
async def test_revoked_session_is_not_reused():
    """A revoked live-looking row must not be handed back — a fresh one is minted."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_session() as db:
        revoked = AppSession(
            id=str(uuid.uuid4()), identity="user@example.com",
            created_at=now, expires_at=now + timedelta(hours=24),
            revoked_at=now,
        )
        db.add(revoked)
    async with db_session() as db:
        got = await _resolve_or_create_app_session(_Req(), db, "user@example.com")
    assert got.id != revoked.id
    assert got.revoked_at is None


@pytest.mark.asyncio
async def test_expired_session_is_not_reused():
    """An expired row must not be handed back — a fresh one is minted."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _resolve_or_create_app_session

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_session() as db:
        expired = AppSession(
            id=str(uuid.uuid4()), identity="user@example.com",
            created_at=now - timedelta(hours=48), expires_at=now - timedelta(hours=1),
            revoked_at=None,
        )
        db.add(expired)
    async with db_session() as db:
        got = await _resolve_or_create_app_session(_Req(), db, "user@example.com")
    assert got.id != expired.id
    assert got.expires_at > now


@pytest.mark.asyncio
async def test_valid_cookie_wins_over_newer_live_session():
    """A valid signed cookie resolving to session X returns X even though a
    newer live session exists for the same identity (reuse must not override
    an explicit, still-valid cookie)."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.auth.middleware import _COOKIE_NAME, _resolve_or_create_app_session, _sign_session_id

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_session() as db:
        older = AppSession(
            id=str(uuid.uuid4()), identity="user@example.com",
            created_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=23),
            revoked_at=None,
        )
        db.add(older)
    async with db_session() as db:
        newer = AppSession(
            id=str(uuid.uuid4()), identity="user@example.com",
            created_at=now, expires_at=now + timedelta(hours=24),
            revoked_at=None,
        )
        db.add(newer)

    class _CookieReq:
        cookies = {_COOKIE_NAME: _sign_session_id(older.id)}

    async with db_session() as db:
        got = await _resolve_or_create_app_session(_CookieReq(), db, "user@example.com")
    assert got.id == older.id, "the cookie's session must win over the newer live session"
