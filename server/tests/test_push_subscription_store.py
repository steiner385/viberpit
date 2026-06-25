from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.mark.asyncio
async def test_upsert_list_delete(tmp_path):
    from server.db import init_db, db_session
    from server.sessions import push_store

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async with db_session() as db:
        await push_store.upsert_subscription(db, endpoint="https://x/1", p256dh="k", auth="a", identity="me")
        await db.commit()
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        assert len(subs) == 1 and subs[0].endpoint == "https://x/1"
        assert subs[0].created_at  # stamped via iso_utc

    # re-upsert same endpoint = idempotent (no duplicate, updates keys)
    async with db_session() as db:
        await push_store.upsert_subscription(db, endpoint="https://x/1", p256dh="k2", auth="a2", identity="me")
        await db.commit()
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        assert len(subs) == 1 and subs[0].p256dh == "k2"

    async with db_session() as db:
        await push_store.delete_subscription(db, "https://x/1")
        await db.commit()
    async with db_session() as db:
        assert await push_store.list_subscriptions(db) == []
