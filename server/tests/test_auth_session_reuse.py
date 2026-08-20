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
