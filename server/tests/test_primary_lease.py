"""DB-side primary lease — "exactly one primary" as a checked invariant (#852).

Unit tests for the fenced acquire/renew/release protocol, plus the orchestrator
wiring: a primary daemon must win the lease to finish start() (a second primary
fails its boot loudly), the heartbeat renews it, and stop() releases it so a
planned failover never waits out the TTL. Workers never touch the lease.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from server.db import db_session, init_db
from server.db.models import DaemonLease
from server.sessions import primary_lease
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


async def _age_lease(seconds: float) -> None:
    """Backdate the lease heartbeat so takeover paths can be tested."""
    from sqlalchemy import update as sa_update
    async with db_session() as db:
        await db.execute(sa_update(DaemonLease).values(
            renewed_at=datetime.utcnow() - timedelta(seconds=seconds)))


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_acquire_renew_release_cycle(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        # First holder starts the fence at 1.
        assert await primary_lease.acquire("dobby", ttl_s=300) == 1
        # A fresh foreign lease refuses a second primary.
        assert await primary_lease.acquire("rocky", ttl_s=300) is None
        # Same-host re-acquisition (crash restart) is instant and bumps the fence.
        assert await primary_lease.acquire("dobby", ttl_s=300) == 2
        # Heartbeat works at the right fence, fails at a stale one.
        assert await primary_lease.renew("dobby", 2) is True
        assert await primary_lease.renew("dobby", 1) is False
        assert await primary_lease.renew("rocky", 2) is False
        # Clean release marks the lease immediately stale (fence keeps counting —
        # a delete would restart at 1 and collide with remembered fences).
        await primary_lease.release("dobby", 2)
        state = await primary_lease.current()
        assert state is not None and state.age_s > 300
        assert await primary_lease.acquire("rocky", ttl_s=300) == 3

    asyncio.run(run())


def test_stale_lease_is_taken_over_fenced(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await primary_lease.acquire("dobby", ttl_s=300) == 1
        await _age_lease(600)  # heartbeat lapsed — holder presumed dead
        fence = await primary_lease.acquire("rocky", ttl_s=300)
        assert fence == 2
        # The fenced-over holder can neither renew nor release the new lease.
        assert await primary_lease.renew("dobby", 1) is False
        await primary_lease.release("dobby", 1)
        state = await primary_lease.current()
        assert state is not None and state.holder == "rocky" and state.fence == 2

    asyncio.run(run())


def test_release_wrong_fence_is_noop(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await primary_lease.acquire("dobby", ttl_s=300) == 1
        await primary_lease.release("dobby", 99)
        state = await primary_lease.current()
        assert state is not None and state.holder == "dobby"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_second_primary_fails_boot_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        # Another host's primary is alive (fresh heartbeat).
        assert await primary_lease.acquire("dobby", ttl_s=300) == 1
        orch = _new_orch()
        with pytest.raises(RuntimeError, match="another primary daemon is alive"):
            await orch.start()
        await orch.stop()  # must be safe after a failed start()

    asyncio.run(run())


def test_primary_start_acquires_and_stop_releases(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()
        state = await primary_lease.current()
        assert state is not None and state.holder == "dobby"
        assert orch._lease_fence == state.fence
        heartbeats = [t for t in asyncio.all_tasks()
                      if t.get_name() == "primary-lease-heartbeat"]
        assert len(heartbeats) == 1
        await orch.stop()
        assert heartbeats[0].done()
        # Released: a different host can take it over immediately (no TTL wait).
        assert await primary_lease.acquire("rocky", ttl_s=300) == state.fence + 1

    asyncio.run(run())


def test_worker_start_never_touches_the_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        # A live primary elsewhere must not block a worker boot.
        assert await primary_lease.acquire("dobby", ttl_s=300) == 1
        orch = _new_orch()
        await orch.start()
        assert orch._lease_fence is None
        assert not [t for t in asyncio.all_tasks()
                    if t.get_name() == "primary-lease-heartbeat"]
        await orch.stop()
        # The foreign lease is untouched.
        state = await primary_lease.current()
        assert state is not None and state.holder == "dobby" and state.fence == 1

    asyncio.run(run())
