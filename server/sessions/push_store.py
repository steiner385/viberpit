"""Persistence for browser Web Push subscriptions."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import PushSubscription
from server.time_iso import iso_utc


async def upsert_subscription(db: AsyncSession, *, endpoint: str, p256dh: str,
                              auth: str, identity: str | None) -> None:
    row = await db.get(PushSubscription, endpoint)
    if row is None:
        db.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth,
                                identity=identity, created_at=iso_utc(datetime.now(timezone.utc).replace(tzinfo=None))))
    else:
        row.p256dh, row.auth, row.identity = p256dh, auth, identity


async def delete_subscription(db: AsyncSession, endpoint: str) -> None:
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))


async def list_subscriptions(db: AsyncSession) -> list[PushSubscription]:
    return list((await db.execute(select(PushSubscription))).scalars().all())
