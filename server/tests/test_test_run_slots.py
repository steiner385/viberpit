"""server.sessions.test_run_slots — verification-phase slot cap (#822).

Real SQLite via init_db(), one asyncio.run() per test (mirrors
test_session_queue_model.py) — no ASGI/TestClient involved here.
"""
from __future__ import annotations

import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_acquire_grants_under_cap(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        granted, ahead = await trs.acquire("s1", "rocky", cap=2)
        assert (granted, ahead) == (True, 0)

    _run(go())


def test_acquire_queues_over_cap(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("s1", "rocky", cap=1))[0] is True
        granted2, ahead2 = await trs.acquire("s2", "rocky", cap=1)
        assert (granted2, ahead2) == (False, 0)
        granted3, ahead3 = await trs.acquire("s3", "rocky", cap=1)
        assert (granted3, ahead3) == (False, 1)

    _run(go())


def test_acquire_is_idempotent_for_the_same_session(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        first = await trs.acquire("s1", "rocky", cap=1)
        second = await trs.acquire("s1", "rocky", cap=1)
        assert first == second == (True, 0)

        await trs.acquire("s2", "rocky", cap=1)  # queued
        waiting_again = await trs.acquire("s2", "rocky", cap=1)
        assert waiting_again == (False, 0)

    _run(go())


def test_release_promotes_oldest_waiter(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.acquire("s1", "rocky", cap=1)
        await trs.acquire("s2", "rocky", cap=1)  # waiting, ahead=0
        await trs.acquire("s3", "rocky", cap=1)  # waiting, ahead=1

        await trs.release("s1")

        assert (await trs.status("s2", "rocky")) == ("holding", 0)
        assert (await trs.status("s3", "rocky")) == ("waiting", 0)

    _run(go())


def test_release_of_a_waiter_does_not_promote_anyone(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.acquire("s1", "rocky", cap=1)
        await trs.acquire("s2", "rocky", cap=1)  # waiting

        await trs.release("s2")

        assert (await trs.status("s1", "rocky")) == ("holding", 0)
        assert (await trs.status("s2", "rocky")) == ("none", 0)

    _run(go())


def test_release_is_idempotent(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await trs.release("never-acquired")  # must not raise
        await trs.acquire("s1", "rocky", cap=1)
        await trs.release("s1")
        await trs.release("s1")  # must not raise

    _run(go())


def test_status_of_unknown_session_is_none(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.status("nope", "rocky")) == ("none", 0)

    _run(go())


def test_hosts_are_independent(tmp_path):
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("s1", "rocky", cap=1))[0] is True
        # Different host, same cap=1: unaffected by rocky's saturation.
        assert (await trs.acquire("s2", "dobby", cap=1))[0] is True

    _run(go())
