"""A snoozed session stops holding a capacity slot once its process is gone.

Snooze is an overlay, not a status, so the status-only slot rule billed a session
someone had put aside for hours against its host's cap. 2026-11-23: three
user-snoozed ``handed_back`` sessions and a self-snoozed ``waiting_external`` one
held 4 of the fleet's 11 slots, every host read "at its session cap", and the
Capacity Governor planned fleet 3 — every product throttled to 1–2 in flight.

The rules pinned here:

* the STATUS rule is unchanged: everything outside CAPACITY_EXEMPT_STATUSES counts,
  including ``handed_back`` / ``waiting_external`` that are NOT snoozed (they keep a
  warm process, and ``waiting_external`` means work is running inside it);
* a snoozed session counts only while it holds a process (a live or spawning driver)
  — so "a live driver is never capacity-exempt" still holds;
* ``_release_snoozed_driver`` stops the process of a snoozed session that is between
  turns with nothing open: never mid-turn, never with a card on the driver, never
  with background work, never a session that is not snoozed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import server.sessions.orchestrator as om
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.engine import AgentEngine
from server.sessions.snooze import build_snooze
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

NOW = datetime.now(timezone.utc)


def _snooze(by="user"):
    return build_snooze(until=(NOW + timedelta(hours=4)).isoformat(),
                        reason="waiting on review", by=by, now=NOW)


def _rec(sid, *, status, snoozed=False, host="dobby"):
    return SessionRecord(id=sid, name=sid, account="acct", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="", cols=80, rows=24, cwd="/tmp", status=status,
                         host=host, snooze=_snooze() if snoozed else None)


class _Driver:
    engine_session_id = None
    last_pr_resolution = None

    def __init__(self, *, turn_active=False, drain_alive=False):
        self.turn_active = turn_active
        self.drain_alive = drain_alive
        self.closed = False

    async def aclose(self):
        self.closed = True


def _primary(records, monkeypatch, *, drivers=(), spawning=()):
    monkeypatch.setattr(om, "self_host_name", lambda: "dobby")
    monkeypatch.setattr(om, "daemon_role", lambda: "primary")
    orch = om.SessionOrchestrator()
    orch._records = {r.id: r for r in records}
    orch._drivers = {sid: _Driver() for sid in drivers}
    orch._spawning = {sid: object() for sid in spawning}

    async def _hosts():
        return frozenset({"dobby"})
    monkeypatch.setattr(orch, "_enabled_host_names", _hosts)
    return orch


def _owned(orch) -> int:
    return asyncio.run(orch.owned_active_session_count())


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["handed_back", "waiting_external", "done", "idle",
                                    "needs_attention"])
def test_a_snoozed_session_with_no_process_holds_no_slot(status, monkeypatch):
    orch = _primary([_rec("s", status=status, snoozed=True)], monkeypatch)
    assert _owned(orch) == 0
    assert orch._active_session_count() == 0   # the admission gate reads the same rule


def test_a_snoozed_session_still_holding_its_process_counts(monkeypatch):
    """Mid-turn, a card open, or background work kept the process: it is real load."""
    orch = _primary([_rec("live", status="waiting_external", snoozed=True),
                     _rec("booting", status="idle", snoozed=True)],
                    monkeypatch, drivers=["live"], spawning=["booting"])
    assert _owned(orch) == 2


@pytest.mark.parametrize("status", ["handed_back", "waiting_external", "in_progress",
                                    "done", "idle", "waiting_question"])
def test_unsnoozed_parked_and_working_statuses_still_count(status, monkeypatch):
    """The status rule is unchanged: only the snooze overlay is new. A handed_back
    or waiting_external session that nobody snoozed keeps its slot whether or not
    a driver is registered right now."""
    orch = _primary([_rec("s", status=status)], monkeypatch)
    assert _owned(orch) == 1


def test_exempt_statuses_stay_exempt_snoozed_or_not(monkeypatch):
    orch = _primary([_rec("c", status="complete", snoozed=True), _rec("q", status="queued"),
                     _rec("p", status="pending_approval")], monkeypatch, drivers=["c"])
    assert _owned(orch) == 0


def test_the_2026_11_23_fleet_shape(monkeypatch):
    """dobby's slice of the incident: 2 working, 2 waiting_external, 1 user-snoozed
    handed_back whose process had already gone. 5/5 → 4/5."""
    orch = _primary([_rec("w1", status="in_progress"), _rec("w2", status="in_progress"),
                     _rec("x1", status="waiting_external"), _rec("x2", status="waiting_external"),
                     _rec("hb", status="handed_back", snoozed=True)],
                    monkeypatch, drivers=["w1", "w2", "x1", "x2"])
    assert _owned(orch) == 4


# ---------------------------------------------------------------------------
# Releasing a snoozed session's process
# ---------------------------------------------------------------------------

async def _real_orch(tmp_path, status="handed_back", snoozed=True):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}")
    async with db_session() as db:
        db.add(DbSession(id="s1", name="s1", account_name="acct1", engine_session_id="c-s1",
                         status=status, cwd="/repo/s1", snooze=_snooze() if snoozed else None))
    orch = om.SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    # rehydrate normalises restart-transient statuses; the live one is what's under test
    orch.record("s1").status = status
    return orch


async def _drain(orch):
    tasks = [t for t in orch._bg_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _run_release(tmp_path, monkeypatch, *, status="handed_back", snoozed=True,
                 bg=False, driver=None):
    async def run():
        orch = await _real_orch(tmp_path, status=status, snoozed=snoozed)
        d = driver or _Driver()
        orch._drivers["s1"] = d

        async def has_bg(sid):
            return bg
        monkeypatch.setattr(orch, "_has_background_work", has_bg)
        released = await orch._release_snoozed_driver("s1")
        count = orch._active_session_count()
        await _drain(orch)
        rec = orch.record("s1")
        result = (released, "s1" in orch._drivers, d.closed, count, rec.status,
                  bool(rec.snooze))
        orch._drivers.pop("s1", None)
        await orch.stop()
        return result
    return asyncio.run(run())


def test_an_idle_snoozed_session_releases_its_process_and_its_slot(tmp_path, monkeypatch):
    released, registered, closed, count, status, still_snoozed = _run_release(
        tmp_path, monkeypatch)
    assert released and not registered and closed
    assert count == 0
    # Nothing else changes: the status and the snooze stay for the wake.
    assert status == "handed_back" and still_snoozed


@pytest.mark.parametrize("kw", [
    {"bg": True},                                       # background work lives in the process
    {"driver": _Driver(turn_active=True)},              # mid-turn (a session's own snooze call)
    {"driver": _Driver(drain_alive=True)},              # turn ended, pump still unwinding
    {"status": "waiting_question"},                     # a card answered through the driver
    {"status": "in_progress"},
    {"snoozed": False},                                 # never touches an awake session
], ids=["background-work", "turn-active", "draining", "card-open", "in-progress",
        "not-snoozed"])
def test_a_snoozed_session_that_is_still_doing_something_keeps_its_process(
        kw, tmp_path, monkeypatch):
    released, registered, closed, count, _status, _snoozed = _run_release(
        tmp_path, monkeypatch, **kw)
    assert not released and registered and not closed
    assert count == 1


def test_the_snooze_sweep_releases_a_not_yet_due_snooze(tmp_path, monkeypatch):
    """A session's own snooze lands mid-turn; the 60 s sweep frees it after."""
    async def run():
        orch = await _real_orch(tmp_path)
        d = _Driver()
        orch._drivers["s1"] = d

        async def no_bg(sid):
            return False
        monkeypatch.setattr(orch, "_has_background_work", no_bg)
        woken = await orch._wake_due_snoozes_once(NOW)
        await _drain(orch)
        result = (woken, "s1" in orch._drivers, d.closed)
        await orch.stop()
        return result
    assert asyncio.run(run()) == (0, False, True)


def test_a_failed_background_scan_keeps_the_process(tmp_path, monkeypatch):
    async def run():
        orch = await _real_orch(tmp_path)

        def boom(sid):
            raise OSError("transcript unreadable")
        monkeypatch.setattr(orch, "transcript_path", boom)
        result = await orch._has_background_work("s1")
        await orch.stop()
        return result
    assert asyncio.run(run()) is True
