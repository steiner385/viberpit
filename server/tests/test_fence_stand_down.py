"""Fenced stand-down (spec 002 FR-202, #949 Task 16).

A daemon whose beat is rejected — the primary bumped its fence — must close its
drivers through the bounded restart close, persist NOTHING for its sessions while
fenced, drop its records, re-register at the new fence and rehydrate only what is still
stamped to it. Drives ``_heartbeat_tick`` directly against a fenced row (no sleeps).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import HostHeartbeat, Session as DbSession
from server.sessions import host_heartbeat
from server.sessions import orchestrator as orchestrator_module
from server.sessions.orchestrator import SessionOrchestrator, self_host_name
from server.sessions.orphaned_asks import CLOSE_REASON_RESTART
from server.tests.fakes.fake_factory import fake_sdk_factory


class _Driver:
    """A live driver stand-in that records how it was closed."""

    def __init__(self):
        self.closed_with: list[str | None] = []

    async def aclose(self, reason=None):
        self.closed_with.append(reason)


def _new_orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


async def _seed(sid: str, *, host: str | None, status: str = "in_progress") -> None:
    async with db_session() as db:
        db.add(DbSession(id=sid, name=sid, account_name="acct", engine_session_id=f"c-{sid}",
                         status=status, cwd=f"/tmp/{sid}", host=host))


async def _fence_bump(host: str) -> None:
    async with db_session() as db:
        await db.execute(sa_update(HostHeartbeat).where(HostHeartbeat.host == host)
                         .values(fence=HostHeartbeat.fence + 1))


async def _row_status(sid: str) -> str:
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession.status).where(DbSession.id == sid))).scalar_one()


@pytest.mark.asyncio
async def test_fenced_tick_stands_down_and_rehydrates_only_own_rows(isolated_db, monkeypatch):
    """Acceptance 2 of the spec: a partitioned host whose sessions were adopted away
    returns, is fenced, and double-drives nothing."""
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    me = self_host_name()
    await _seed("mine", host=me)             # still stamped here → comes back
    await _seed("taken", host="dobby")       # adopted away → must NOT come back
    orch = _new_orch()
    orch._hb_fence = await host_heartbeat.register(me)
    await orch.rehydrate()
    assert set(orch._records) == {"mine"}
    live = _Driver()
    orch._drivers["mine"] = live
    orch._records["taken"] = orch._records["mine"]   # a stale copy the fence must drop
    # Status writes attempted WHILE fenced must not reach the DB: the driver's close
    # emits a status through _persist_status; capture every row write.
    writes: list[tuple[str, str]] = []
    real_update = orchestrator_module.SessionOrchestrator._update_session_row

    async def spy_update(self, sid, values):
        writes.append((sid, str(values)))
        return await real_update(self, sid, values)
    monkeypatch.setattr(orchestrator_module.SessionOrchestrator, "_update_session_row", spy_update)

    await _fence_bump(me)
    await orch._heartbeat_tick()          # beat rejected → _on_fenced

    # (2) drivers closed as a RESTART (asks settle "interrupted", not refused)
    assert live.closed_with == [CLOSE_REASON_RESTART]
    assert orch._drivers == {}
    # (4) re-registered at the NEW fence, heartbeat live again
    snap = (await host_heartbeat.snapshot()).hosts[me]
    assert snap.fence == 2 and orch._hb_fence == 2 and orch._hb_fenced is False
    assert await orch._heartbeat_tick() is None    # a normal beat works again
    # (5) rehydrate reloaded only rows still stamped here
    assert set(orch._records) == {"mine"}
    # (1) a fenced daemon persists nothing: the row kept its pre-fence status
    assert await _row_status("mine") == "in_progress"
    assert writes == []
    await orch.stop()


@pytest.mark.asyncio
async def test_persist_and_prompt_are_inert_while_fenced(isolated_db):
    orch = _new_orch()
    await _seed("s1", host=self_host_name())
    await orch.rehydrate()
    orch._hb_fenced = True
    orch._publish_event = lambda *_a, **_k: None   # type: ignore[method-assign]
    assert await orch._persist_status("s1", "done") == "done"
    assert await _row_status("s1") == "in_progress"      # untouched
    assert orch._records["s1"].status == "idle"          # rehydrate's normalization, not ours
    assert await orch.send_prompt("s1", "hello") == "draining"
    orch._dirty_ids().add("s1")
    assert await orch._flush_dirty_status() == 0
    await orch.stop()


@pytest.mark.asyncio
async def test_on_fenced_survives_a_failing_reregister(isolated_db, monkeypatch):
    """NFR-5: the stand-down never raises into the heartbeat loop, and a DB blip on
    re-register leaves the daemon un-fenced with no fence so the next tick retries."""
    orch = _new_orch()
    orch._hb_fenced = True
    orch._hb_fence = None
    monkeypatch.setattr(host_heartbeat, "register", AsyncMock(side_effect=RuntimeError("db")))
    await orch._on_fenced()
    assert orch._hb_fenced is False and orch._hb_fence is None
    await orch.stop()


@pytest.mark.asyncio
async def test_stop_and_stand_down_share_the_bounded_close(isolated_db):
    """One close path (constitution VI): ``stop()`` and ``_on_fenced`` both go through
    ``_close_live_drivers_bounded``."""
    orch = _new_orch()
    d1, d2 = _Driver(), _Driver()
    orch._drivers.update({"a": d1, "b": d2})
    assert await orch._close_live_drivers_bounded(phase="t") == 2
    assert d1.closed_with == d2.closed_with == [CLOSE_REASON_RESTART]
    assert orch._drivers == {}
    await orch.stop()
