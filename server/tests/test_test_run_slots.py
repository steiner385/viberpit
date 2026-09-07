"""server.sessions.test_run_slots — verification-phase slot cap (#822).

Real SQLite via init_db(), one asyncio.run() per test (mirrors
test_session_queue_model.py) — no ASGI/TestClient involved here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


def _run(coro):
    return asyncio.run(coro)


async def _make_stale(session_id: str) -> None:
    """Backdate this session's row past the stale-hold TTL, so it looks like a
    holder that died without ever running its release. Written directly because
    acquire() always stamps requested_at with "now"."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import TestRunSlot
    from server.sessions import test_run_slots as trs

    async with db_session() as db:
        row = (await db.execute(
            select(TestRunSlot).where(TestRunSlot.session_id == session_id)
        )).scalar_one()
        row.requested_at = (datetime.now(timezone.utc)
                            - timedelta(seconds=trs._STALE_HOLD_S + 100))


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


def test_stale_holder_is_reaped_on_the_next_acquire(tmp_path):
    """A session killed before its release trap ran must not hold the host's
    only slot forever — the next acquire() reaps it and takes the slot."""
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("dead-session", "rocky", cap=1))[0] is True
        await _make_stale("dead-session")  # died; release() never ran

        assert (await trs.acquire("new-session", "rocky", cap=1)) == (True, 0)
        assert (await trs.status("dead-session", "rocky")) == ("none", 0)

    _run(go())


def test_waiter_self_promotes_once_a_stale_holder_is_reaped(tmp_path):
    """The case release()'s promotion cannot reach: the holder died, so nobody
    ever calls release() — the waiter's own next acquire() must promote it."""
    from server.db import init_db
    from server.sessions import test_run_slots as trs

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        assert (await trs.acquire("s1", "rocky", cap=1))[0] is True
        assert (await trs.acquire("s2", "rocky", cap=1)) == (False, 0)
        await _make_stale("s1")  # died holding the only slot; no release()

        assert (await trs.acquire("s2", "rocky", cap=1)) == (True, 0)
        assert (await trs.status("s2", "rocky")) == ("holding", 0)

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
