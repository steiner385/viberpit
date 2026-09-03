"""Orchestrator per-host fenced heartbeat loop (spec 002 FR-101, Task 4).

Drives _heartbeat_tick() directly (no start() call, no real sleep) so tests
are fast and deterministic. Uses the isolated_db fixture for a private SQLite
schema and fake_sdk_factory to satisfy the orchestrator constructor.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.sessions import host_heartbeat
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

    snap = await host_heartbeat.snapshot()
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
    snap = await host_heartbeat.snapshot()
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
