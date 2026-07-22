"""Pure session-queue CRUD: ordering, moves, renumbering."""
import asyncio


async def _seed(n):
    from server.sessions import session_queue as sq
    for i in range(1, n + 1):
        await sq.enqueue(f"s{i}", {"n": i})


def _run(tmp_path, coro_fn):
    from server.db import init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
        await coro_fn()

    asyncio.run(run())


def test_enqueue_appends_and_next_is_fifo(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        p1 = await sq.enqueue("s1", {})
        p2 = await sq.enqueue("s2", {})
        assert (p1, p2) == (1, 2)
        head = await sq.next_entry()
        assert head.session_id == "s1"
    _run(tmp_path, body)


def test_move_up_down_top_and_positions(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(4)                                   # s1 s2 s3 s4
        assert await sq.move("s3", "up")                 # s1 s3 s2 s4
        assert await sq.move("s4", "top")                # s4 s1 s3 s2
        assert await sq.move("s1", "down")               # s4 s3 s1 s2
        assert await sq.move("s2", "position", 2)        # s4 s2 s3 s1
        assert (await sq.positions()) == {"s4": 1, "s2": 2, "s3": 3, "s1": 4}
    _run(tmp_path, body)


def test_move_bounds_are_noops(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(2)
        assert await sq.move("s1", "up")        # already head → True, unchanged
        assert (await sq.positions())["s1"] == 1
        assert not await sq.move("ghost", "up")  # unknown id → False
    _run(tmp_path, body)


def test_remove(tmp_path):
    async def body():
        from server.sessions import session_queue as sq
        await _seed(2)
        assert await sq.remove("s1")
        assert not await sq.remove("s1")
        assert (await sq.positions()) == {"s2": 1}
    _run(tmp_path, body)
