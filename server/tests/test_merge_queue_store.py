# server/tests/test_merge_queue_store.py
import asyncio
from server.db import init_db
from server.github.merge_queue import QueueEntry
from server.pipeline import merge_queue_store as store


def _e(num, oid="h", checks="pending"):
    return QueueEntry(pr_number=num, position=1, state="AWAITING_CHECKS", enqueued_at=0,
                      batch_head_oid=oid, group_checks=checks, checks_total=4,
                      checks_done=None, eta_seconds=None)


def test_record_then_window_round_trips(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1), _e(2, oid="b")))
        await store.record("r", 200, (_e(1),))
        win = await store.window("r", since_epoch=0)
        assert [o.captured_at for o in win] == [100, 200]      # ascending
        assert {e.pr_number for e in win[0].entries} == {1, 2}
        assert win[0].entries[0].batch_head_oid in {"h", "b"}
    asyncio.run(body())


def test_window_filters_by_since(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1),))
        await store.record("r", 500, (_e(1),))
        win = await store.window("r", since_epoch=300)
        assert [o.captured_at for o in win] == [500]
    asyncio.run(body())


def test_prune_drops_old_rows(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1),))
        await store.record("r", 9000, (_e(1),))
        deleted = await store.prune(older_than_epoch=1000)
        assert deleted == 1
        assert [o.captured_at for o in await store.window("r", 0)] == [9000]
    asyncio.run(body())
