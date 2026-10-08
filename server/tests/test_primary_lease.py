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


def test_every_lease_log_line_names_the_lease(tmp_path, caplog):
    """``session_lease.py`` reuses these functions with a PER-SESSION name, so a
    line that only says "primary-lease: acquired by 'rocky'" reads as a worker
    illegally taking the primary lease. Every line must name its lease (#2106)."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        with caplog.at_level("INFO", logger="server.sessions.primary_lease"):
            fence = await primary_lease.acquire("rocky", ttl_s=300, name="session:abc")
            assert fence is not None
            await primary_lease.release("rocky", fence, name="session:abc")

        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 2, lines
        for line in lines:
            assert "session:abc" in line, line
            # The misleading half: never a bare "primary-lease: <verb> by".
            assert "name=" in line, line

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
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "rocky")

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
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

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
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "rocky")

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


def test_worker_start_skips_fleet_global_loops(tmp_path, monkeypatch):
    """#919: a worker serves dispatched sessions only — it must NOT start the
    fleet-global automation loops, or two daemons double-drive the shared DB."""
    from server.sessions.orchestrator import _PRIMARY_ONLY_LOOP_ATTRS
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "rocky")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()
        still_running = {a for a in _PRIMARY_ONLY_LOOP_ATTRS
                         if getattr(orch, a, None) is not None}
        assert orch._autopilot_task is not None
        await orch.stop()
        assert not still_running, f"worker started fleet-global loops: {sorted(still_running)}"
        # Per-host local loops still run (the janitor owns this host's worktrees).
        assert orch._janitor_task is None  # stop() cleared it, proving it existed

    asyncio.run(run())


def test_primary_start_runs_every_fleet_global_loop(tmp_path, monkeypatch):
    """The mirror of the worker case: a primary starts ALL of them, so
    _PRIMARY_ONLY_LOOP_ATTRS stays in lockstep with start()."""
    from server.sessions.orchestrator import _PRIMARY_ONLY_LOOP_ATTRS
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()
        missing = {a for a in _PRIMARY_ONLY_LOOP_ATTRS
                   if getattr(orch, a, None) is None}
        await orch.stop()
        assert not missing, f"primary did not start: {sorted(missing)}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A failed renew is not automatically a two-host split-brain (#2146, mirrors
# the #1844 fix for the per-session lease)
# ---------------------------------------------------------------------------


def test_a_same_host_takeover_is_not_reported_as_two_primaries(tmp_path, monkeypatch, caplog):
    """All production occurrences of the old message named the REPORTING host as
    the thief ("fenced takeover by 'dobby'" logged on dobby), which is by
    construction not two primaries — it is another incarnation of this daemon
    (e.g. an overlapping restart). Say that instead of raising the fleet-wide
    alarm that trains an operator to "fix" a VIBERPIT_DAEMON_ROLE that was never
    wrong."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()
        stale_fence = orch._lease_fence
        # Another process on THIS host re-acquires (an overlapping restart).
        assert await primary_lease.acquire("dobby", ttl_s=300) == stale_fence + 1

        with caplog.at_level("DEBUG", logger="server.sessions.orchestrator"):
            await orch._primary_lease_beat()
        await orch.stop()

        assert not [r for r in caplog.records if r.levelname == "CRITICAL"]
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("primary-lease" in m and "'dobby'" in m for m in warnings), warnings
        assert not any("TWO primary" in m for m in warnings), warnings

    asyncio.run(run())


def test_a_real_foreign_takeover_still_screams(tmp_path, monkeypatch, caplog):
    """The alarm #852 exists for. Narrowing it must not silence it: a DIFFERENT
    host holding the lease is still CRITICAL, unchanged."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()
        await _age_lease(600)
        assert await primary_lease.acquire("rocky", ttl_s=300) is not None

        with caplog.at_level("DEBUG", logger="server.sessions.orchestrator"):
            await orch._primary_lease_beat()
        await orch.stop()

        screams = [r.getMessage() for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(screams) == 1, screams
        assert "'rocky'" in screams[0] and "TWO primary" in screams[0]

    asyncio.run(run())


@pytest.mark.parametrize("exc,expect_warning", [
    (ConnectionRefusedError("db down"), False),
    (RuntimeError("no such column"), True),
])
def test_renew_db_failure_warns_only_when_not_transient(
        tmp_path, monkeypatch, caplog, exc, expect_warning):
    """A connect blip on renew is retried next beat quietly; a real DB defect
    keeps its WARNING (#2791)."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        orch = _new_orch()
        await orch.start()

        async def boom(*a, **k):
            raise exc
        monkeypatch.setattr(primary_lease, "renew", boom)
        with caplog.at_level("DEBUG", logger="server.sessions.orchestrator"):
            await orch._primary_lease_beat()
        await orch.stop()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and "renew failed" in r.getMessage()]
        assert bool(warnings) is expect_warning

    asyncio.run(run())
