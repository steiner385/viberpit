# server/tests/test_merge_queue_store.py
import asyncio
from sqlalchemy import event, insert

from server.db import db_session, init_db
from server.db.models import MergeQueueObservation
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


def test_empty_queue_records_empty_observation(tmp_path):
    """A poll that finds the queue EMPTY must still leave a trace: without it,
    window()'s newest observation stays the last busy snapshot and the dashboard
    replays a merged train for up to 24h (the #11778 "building 94%" ghost)."""
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1), _e(2)))
        await store.record("r", 200, ())
        win = await store.window("r", since_epoch=0)
        assert [o.captured_at for o in win] == [100, 200]
        assert {e.pr_number for e in win[0].entries} == {1, 2}
        assert win[1].entries == ()  # queue seen empty — newest truth
    asyncio.run(body())


def test_empty_observation_prunes_like_any_row(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, ())
        await store.record("r", 9000, ())
        deleted = await store.prune(older_than_epoch=1000)
        assert deleted == 1
        assert [o.captured_at for o in await store.window("r", 0)] == [9000]
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


def test_large_window_yields_during_loading_and_grouping(tmp_path, monkeypatch):
    """A ready callback must run between bounded amounts of ORM and Python work."""
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        # A single timestamp spans multiple batches; other timestamps include
        # sentinel-only and mixed sentinel/entry observations.
        rows = [dict(repo="r", captured_at=100, pr_number=n) for n in range(1, 2049)]
        rows += [dict(repo="r", captured_at=t, pr_number=t) for t in range(200, 2248)]
        rows += [dict(repo="r", captured_at=3000, pr_number=None),
                 dict(repo="r", captured_at=100, pr_number=None),
                 dict(repo="other", captured_at=100, pr_number=9999),
                 dict(repo="r", captured_at=0, pr_number=9999)]
        async with db_session() as session:
            await session.execute(insert(MergeQueueObservation), rows)
            await session.commit()

        loop = asyncio.get_running_loop()
        counts = {"loaded": 0, "grouped": 0}
        maximum = counts.copy()

        def checkpoint(kind):
            counts[kind] = 0

        def work(kind):
            if counts[kind] == 0:
                loop.call_soon(checkpoint, kind)
            counts[kind] += 1
            maximum[kind] = max(maximum[kind], counts[kind])

        def loaded(_row, _context):
            work("loaded")

        original_entry = store._entry

        def entry(row):
            work("grouped")
            return original_entry(row)

        monkeypatch.setattr(store, "_entry", entry)
        event.listen(MergeQueueObservation, "load", loaded)
        try:
            win = await store.window("r", since_epoch=100)
        finally:
            event.remove(MergeQueueObservation, "load", loaded)

        assert [o.captured_at for o in win] == [100, *range(200, 2248), 3000]
        assert {e.pr_number for e in win[0].entries} == set(range(1, 2049))
        assert len(win[0].entries) == 2048
        assert all(o.entries[0].pr_number == o.captured_at for o in win[1:-1])
        assert win[-1].entries == ()
        # An independent upper bound, not an assertion of the implementation's
        # exact batch size. execute().scalars().all() exceeds it in both phases.
        assert 0 < maximum["loaded"] <= 512
        assert 0 < maximum["grouped"] <= 512

    asyncio.run(body())
