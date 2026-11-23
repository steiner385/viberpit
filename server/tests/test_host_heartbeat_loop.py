"""Orchestrator per-host fenced heartbeat loop (spec 002 FR-101, Task 4).

Drives _heartbeat_tick() directly (no start() call, no real sleep) so tests
are fast and deterministic. Uses the isolated_db fixture for a private SQLite
schema and fake_sdk_factory to satisfy the orchestrator constructor.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.db import dispose_db, init_db
from server.sessions import host_heartbeat
from server.sessions import orchestrator as orchestrator_module
from server.sessions.orchestrator import SessionOrchestrator, self_host_name
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


# ---------------------------------------------------------------------------
# 1. A tick with a valid fence beats the row (fence unchanged, row present).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_beats_registered_row(isolated_db):
    orch = _new_orch()
    orch._hb_fence = await host_heartbeat.register(self_host_name())

    await orch._heartbeat_tick()

    snap = (await host_heartbeat.snapshot()).hosts
    assert self_host_name() in snap, "host row must be present after a tick"
    # The fence must not change on a successful beat.
    assert snap[self_host_name()].fence == orch._hb_fence


# ---------------------------------------------------------------------------
# 2. A rejected beat (fence mismatch) invokes _on_fenced exactly once and
#    nulls _hb_fence so the next tick is a no-op.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_calls_on_fenced_once_when_beat_rejected(isolated_db, monkeypatch):
    orch = _new_orch()
    orch._hb_fence = await host_heartbeat.register(self_host_name())

    # Make beat() return False (simulates fence bump by an external caller).
    monkeypatch.setattr(host_heartbeat, "beat", AsyncMock(return_value=False))

    on_fenced_mock = AsyncMock()
    monkeypatch.setattr(orch, "_on_fenced", on_fenced_mock)

    await orch._heartbeat_tick()
    on_fenced_mock.assert_awaited_once()
    assert orch._hb_fence is None, "_hb_fence must be cleared after a fenced beat"

    # Second tick: fence is None, so _on_fenced must NOT be called again.
    await orch._heartbeat_tick()
    on_fenced_mock.assert_awaited_once()  # still just once


# ---------------------------------------------------------------------------
# 3. A DB error in beat() is swallowed — fence is unchanged and no raise.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_swallows_db_error(isolated_db, monkeypatch):
    orch = _new_orch()
    orch._hb_fence = 1

    async def _raise(*_a, **_kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(host_heartbeat, "beat", _raise)

    # Must not raise.
    await orch._heartbeat_tick()

    # Fence must be left unchanged (retry on next beat, not a stand-down).
    assert orch._hb_fence == 1


# ---------------------------------------------------------------------------
# 4. A daemon whose initial register() failed (DB blip at boot) must RETRY on
#    the loop. Without this, one transient error at start() silently disables
#    this host's heartbeat for the whole process lifetime, and every session it
#    owns eventually reads "unreachable" on a perfectly healthy host.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_retries_register_when_never_registered(isolated_db):
    orch = _new_orch()
    orch._hb_fence = None        # start()'s register() raised
    orch._hb_fenced = False      # ...but we were never fenced

    await orch._heartbeat_tick()

    assert orch._hb_fence is not None, "tick must re-register after a failed boot register"
    snap = (await host_heartbeat.snapshot()).hosts
    assert self_host_name() in snap


# ---------------------------------------------------------------------------
# 5. ...but a FENCED daemon must stay inert. Re-registering would adopt the
#    bumped fence and resurrect a daemon that never stood down — the exact
#    thing Phase 4 (Task 16) has to gate. Same sentinel, opposite requirement.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_does_not_reregister_after_being_fenced(isolated_db, monkeypatch):
    orch = _new_orch()
    orch._hb_fence = None
    orch._hb_fenced = True       # a prior beat was rejected

    register_mock = AsyncMock(return_value=99)
    monkeypatch.setattr(host_heartbeat, "register", register_mock)

    await orch._heartbeat_tick()

    register_mock.assert_not_awaited()
    assert orch._hb_fence is None, "a fenced daemon must not silently re-acquire a fence"


# ---------------------------------------------------------------------------
# 6-8. A heartbeat DB call that never RETURNS (as opposed to raising) must be
#      bounded. A host hard-reset mid-beat leaves its old Postgres backend "idle in
#      transaction" holding the host_heartbeats row lock; the server never notices
#      the dead peer, so the restarted daemon's register() INSERT queued behind that
#      lock forever. start() runs inside the daemon lifespan, BEFORE uvicorn binds,
#      so rocky stayed off :9443 for 18 min with every session "unreachable"
#      (2026-11-23). A bounded call turns the hang into the DB-error path that
#      already exists: boot continues, the loop retries each beat.
# ---------------------------------------------------------------------------

async def _hang_forever(*_a, **_kw):
    await asyncio.Event().wait()


@pytest.fixture
def _fast_heartbeat(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "heartbeat_interval_s", lambda: 0.05)


@pytest.mark.asyncio
async def test_start_is_not_held_hostage_by_a_hung_register(tmp_path, monkeypatch,
                                                            _fast_heartbeat):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setattr(host_heartbeat, "register", _hang_forever)
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'hb.db'}")
    orch = _new_orch()
    try:
        await asyncio.wait_for(orch.start(), timeout=5.0)
        assert orch._hb_fence is None, "a timed-out register leaves us un-registered"
        assert not orch._hb_fenced, "...and NOT fenced, so the loop retries it"
    finally:
        await orch.stop()
        await dispose_db()


@pytest.mark.asyncio
async def test_tick_bounds_a_hung_register(isolated_db, monkeypatch, _fast_heartbeat):
    orch = _new_orch()
    orch._hb_fence = None
    orch._hb_fenced = False
    monkeypatch.setattr(host_heartbeat, "register", _hang_forever)

    await asyncio.wait_for(orch._heartbeat_tick(), timeout=5.0)

    assert orch._hb_fence is None
    assert not orch._hb_fenced


@pytest.mark.asyncio
async def test_tick_bounds_a_hung_beat(isolated_db, monkeypatch, _fast_heartbeat):
    orch = _new_orch()
    orch._hb_fence = 1
    on_fenced_mock = AsyncMock()
    monkeypatch.setattr(orch, "_on_fenced", on_fenced_mock)
    monkeypatch.setattr(host_heartbeat, "beat", _hang_forever)

    await asyncio.wait_for(orch._heartbeat_tick(), timeout=5.0)

    assert orch._hb_fence == 1, "a timed-out beat is a DB error, not a fence rejection"
    on_fenced_mock.assert_not_awaited()
