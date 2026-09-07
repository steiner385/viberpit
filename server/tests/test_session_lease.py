"""Per-session ownership lease — "at most one live CLI per session id" (#1379).

A deploy-driven daemon restart resumed the SAME session on BOTH fleet hosts: each
rehydrated the shared row, each concluded it owned it, and each spawned its own
``claude --resume <conversation-id>`` into the same worktree. The twins then raced
on git and split the session's account and worktree.

These tests pin the three things that make that impossible:

* the lease protocol itself (a live foreign holder blocks; the SAME host always
  re-acquires, so a systemd restart is never delayed; a lapsed holder is fenced out);
* the GATE — ``ensure_driver`` refuses to build a driver for a leased session, and
  the spawn path raises. A lease nobody consults is decoration;
* the lifecycle — a driver teardown hands the lease back immediately, and ``stop()``
  releases everything, so a planned restart hands its sessions over rather than
  fencing them off for a whole TTL.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from server.db import db_session, dispose_db, init_db
from server.db.models import DaemonLease
from server.sessions import session_lease
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionLeaseHeld, SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

SID = "11111111-2222-3333-4444-555555555555"


def _new_orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _record(sid: str = SID, **kw) -> SessionRecord:
    """A detached-but-resumable record — the post-restart shape both hosts saw."""
    fields = dict(
        id=sid, name="rebrand", account="acct-a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="f9eaab62-4465-4651-8425-f42645b92ef7",
        cols=80, rows=24, cwd="/tmp/ccswitch-lease-test", status="idle",
    )
    fields.update(kw)
    return SessionRecord(**fields)


async def _age_lease(sid: str, seconds: float) -> None:
    """Backdate a session lease's heartbeat so takeover paths can be tested."""
    from sqlalchemy import update as sa_update
    async with db_session() as db:
        await db.execute(
            sa_update(DaemonLease)
            .where(DaemonLease.name == session_lease.lease_name(sid))
            .values(renewed_at=datetime.utcnow() - timedelta(seconds=seconds)))


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_lease_is_per_session_not_fleet_wide(tmp_path):
    """Two different sessions never contend: the row key carries the session id."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await session_lease.acquire("sess-a", "dobby", ttl_s=120) == 1
        # A different session on ANOTHER host is untouched by the first claim.
        assert await session_lease.acquire("sess-b", "rocky", ttl_s=120) == 1
        assert session_lease.session_id_of(session_lease.lease_name("sess-a")) == "sess-a"
        assert session_lease.session_id_of("primary") is None

    asyncio.run(run())


def test_live_twin_is_refused_but_same_host_always_re_acquires(tmp_path):
    """The core invariant. dobby holds the session; rocky is refused while dobby's
    heartbeat is fresh. dobby's OWN restart re-takes it instantly — a systemd
    restart must never have to wait out the TTL to resume its own sessions."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) == 1
        assert await session_lease.acquire(SID, "rocky", ttl_s=120) is None
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) == 2
        # The fenced-out peer cannot heartbeat its way back in.
        assert await session_lease.renew(SID, "rocky", 1) is False
        assert await session_lease.renew(SID, "dobby", 2) is True

    asyncio.run(run())


def test_a_dead_holders_session_is_taken_over_after_the_ttl(tmp_path):
    """The lease may not strand a session on a host that never comes back."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) == 1
        await _age_lease(SID, 600)
        assert await session_lease.acquire(SID, "rocky", ttl_s=120) == 2
        state = await session_lease.current(SID)
        assert state is not None and state.holder == "rocky"

    asyncio.run(run())


def test_release_hands_over_without_waiting_out_the_ttl(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) == 1
        await session_lease.release(SID, "dobby", 1)
        assert await session_lease.acquire(SID, "rocky", ttl_s=120) == 2

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The gate: a lease nobody consults is decoration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_driver_refuses_a_session_a_live_peer_holds(tmp_path, monkeypatch):
    """THE regression test for #1379: this is the call both hosts made after the
    21:34 restart, and the second one must now come back empty-handed."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) == 1
        orch = _new_orch()
        orch._records[SID] = _record()

        assert await orch.ensure_driver(SID) is None
        assert SID not in orch._drivers, "a driver was built for a leased session"
        # We must not have stolen dobby's claim on the way out.
        state = await session_lease.current(SID)
        assert state is not None and state.holder == "dobby" and state.fence == 1
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_ensure_driver_proceeds_once_the_holder_goes_stale(tmp_path, monkeypatch):
    """The mirror: the gate must not permanently strand a session on a dead host.
    Past the TTL the surviving host claims it and gets on with the build."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        await session_lease.acquire(SID, "dobby", ttl_s=120)
        await _age_lease(SID, 600)
        orch = _new_orch()
        orch._records[SID] = _record()

        assert await orch._claim_session(SID, why="resume") is True
        state = await session_lease.current(SID)
        assert state is not None and state.holder == "rocky"
        assert orch._session_lease_fences[SID] == state.fence
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_spawn_over_a_leased_session_raises(tmp_path, monkeypatch):
    """The spawn path (queue promotion / spawn-over-existing-conversation) refuses
    loudly rather than returning a session the caller would treat as started."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        await session_lease.acquire(SID, "dobby", ttl_s=120)
        orch = _new_orch()
        with pytest.raises(SessionLeaseHeld) as excinfo:
            await orch._spawn_session(
                SID, name="twin", account="acct-a", cwd="/tmp/ccswitch-lease-test",
                initial_prompt=None, cols=80, rows=24, branch=None, repo=None,
                identity="system", permission_mode="interactive", attachment_ids=None,
                name_is_auto=False, origin=None, origin_ref=None, model=None)
        assert excinfo.value.session_id == SID
        assert SID not in orch._records, "a refused spawn left a pre-seeded record"
        assert SID not in orch._drivers
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_a_db_error_fails_open(tmp_path, monkeypatch):
    """The lease is a safety net over a rare race — a database blip must never stop
    a whole fleet from running sessions."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        async def boom(*a, **kw):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(session_lease, "acquire", boom)
        orch = _new_orch()
        assert await orch._claim_session(SID, why="resume") is True
        assert SID not in orch._session_lease_fences
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_enforcement_kill_switch_allows_a_forced_takeover(tmp_path, monkeypatch):
    """An operator who must force a takeover before the TTL sets the kill switch;
    the claim is still logged and attempted, it just stops being fatal."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    monkeypatch.setenv("CCSWITCH_SESSION_LEASE_ENFORCE", "false")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        await session_lease.acquire(SID, "dobby", ttl_s=120)
        orch = _new_orch()
        assert await orch._claim_session(SID, why="resume") is True
    finally:
        await dispose_db()


# ---------------------------------------------------------------------------
# Lifecycle: the lease must be given back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_teardown_hands_the_lease_back_immediately(tmp_path, monkeypatch):
    """Closing / archiving / idle-reaping a session frees it for another host at
    once, instead of fencing it off for a whole SESSION_LEASE_TTL_S."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        orch = _new_orch()
        assert await orch._claim_session(SID, why="spawn") is True

        await orch._teardown_driver_state(SID)

        assert SID not in orch._session_lease_fences
        assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_the_renew_loop_releases_leases_whose_driver_is_gone(tmp_path, monkeypatch):
    """Reconciling against `_drivers` is what makes a leaked lease self-heal: no
    teardown path has to remember to release, absence of a driver IS the signal."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        orch = _new_orch()
        await orch._claim_session("kept", why="spawn")
        await orch._claim_session("dropped", why="spawn")
        orch._drivers["kept"] = object()   # only this one still has a live CLI

        await orch._session_lease_tick()
        orch._drivers.pop("kept", None)

        assert "dropped" not in orch._session_lease_fences
        assert "kept" in orch._session_lease_fences
        # Released for real, not just forgotten locally.
        assert await session_lease.acquire("dropped", "rocky", ttl_s=120) is not None
        assert await session_lease.acquire("kept", "rocky", ttl_s=120) is None
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_stop_releases_every_session_lease(tmp_path, monkeypatch):
    """A planned restart (the deploy path the incident came from) must hand its
    sessions over, not fence the peer off them for the TTL."""
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        orch = _new_orch()
        await orch.start()
        await orch._claim_session(SID, why="spawn")
        heartbeats = [t for t in asyncio.all_tasks()
                      if t.get_name() == "session-lease-heartbeat"]
        assert len(heartbeats) == 1

        await orch.stop()

        assert heartbeats[0].done()
        assert not orch._session_lease_fences
        assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    finally:
        await dispose_db()
