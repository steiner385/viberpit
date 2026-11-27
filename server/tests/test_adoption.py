"""The primary's adoption pass (spec 002 FR-201/FR-203/FR-205, #949 Task 18):
``server/sessions/adoption.py`` against a private SQLite DB and real heartbeat rows.

No sleeps: heartbeat ages are set by rewriting ``beat_at``; the pass is driven directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import Host, HostHeartbeat, Session as DbSession
from server.sessions import adoption, host_heartbeat, provenance
from server.sessions import orchestrator as orchestrator_module
from server.sessions.fleet import HostCandidate
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

DEAD_AFTER = 300.0


async def _age(host: str, seconds: float) -> None:
    async with db_session() as db:
        await db.execute(sa_update(HostHeartbeat).where(HostHeartbeat.host == host)
                         .values(beat_at=datetime.utcnow() - timedelta(seconds=seconds)))


async def _host_row(name: str, *, healthy=True, enabled=True, active=0, cap=None) -> None:
    async with db_session() as db:
        db.add(Host(name=name, endpoint=f"https://{name}:8443", enabled=enabled,
                    healthy=healthy, active_sessions=active, max_sessions=cap))


async def _session(sid: str, *, host: str, status: str = "in_progress", **kw) -> None:
    fields = dict(id=sid, name=f"goal {sid}", account_name="acct", engine_session_id=f"c-{sid}",
                  status=status, cwd=f"/tmp/{sid}", host=host)
    fields.update(kw)
    async with db_session() as db:
        db.add(DbSession(**fields))


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalar_one()


def _primary(monkeypatch, name: str = "w2") -> SessionOrchestrator:
    monkeypatch.setenv("VIBERPIT_HOST_NAME", name)
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "primary")
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


# ── find_dead_hosts ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_dead_hosts_wants_stale_beat_and_an_owned_session(isolated_db):
    for h in ("w1", "w3", "w4", "me"):
        await host_heartbeat.register(h)
    await _age("w1", 999)                # stale, owns a live session → dead
    await _age("w3", 999)                # stale, owns only an ended session → not listed
    await _age("me", 999)                # stale but excluded (the caller)
    await _session("a", host="w1")
    await _session("b", host="w3", status="ended")
    await _session("c", host="w4")       # fresh beat → never
    await _session("d", host="me")
    dead = await adoption.find_dead_hosts(dead_after_s=DEAD_AFTER, exclude=frozenset({"me"}))
    assert [d.host for d in dead] == ["w1"]


@pytest.mark.asyncio
async def test_find_dead_hosts_is_empty_when_everything_beats(isolated_db):
    await host_heartbeat.register("w1")
    await _session("a", host="w1")
    assert await adoption.find_dead_hosts(dead_after_s=DEAD_AFTER) == []


# ── adopt_host ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adopt_host_fences_first_then_restamps_each_session_with_provenance(
        isolated_db, monkeypatch):
    orch = _primary(monkeypatch)
    try:
        fence = await host_heartbeat.register("w1")
        await host_heartbeat.register("w2")
        await _age("w1", 999)
        await _host_row("w2", active=1, cap=6)
        await _session("a", host="w1", status="in_progress")
        await _session("b", host="w1", status="waiting_question")
        await _session("c", host="w1", status="done")          # resting: adopted, not normalized
        await _session("z", host="w1", status="archived")      # terminal: never touched
        await _session("m", host="w1", status="idle", moving_to="w2")   # mid-move: the saga's
        # The primary's dashboard mirror of a row must follow the restamp.
        await orch.rehydrate()
        hb = (await host_heartbeat.snapshot()).hosts["w1"]

        n = await adoption.adopt_host(orch, hb, dead_after_s=DEAD_AFTER)

        assert n == 3
        assert (await host_heartbeat.snapshot()).hosts["w1"].fence == fence + 1
        for sid, before, after in (("a", "in_progress", "idle"), ("b", "waiting_question", "idle"),
                                   ("c", "done", "done")):
            row = await _row(sid)
            assert (row.host, row.status, row.adopted_from, row.moved_from, row.pid) == (
                "w2", after, "w1", "w1", None), sid
            assert row.moved_at is not None
            kinds = [(e["kind"], e["detail"]) for e in await provenance.list_events(sid)]
            assert (provenance.INTERRUPTED,
                    {"kind": "host_lost", "fromStatus": before, "host": "w1"}) in kinds
            assert orch._records[sid].host == "w2" and orch._records[sid].status == after
        assert (await _row("z")).host == "w1" and (await _row("z")).adopted_from is None
        assert (await _row("m")).host == "w1" and (await _row("m")).adopted_from is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_second_racing_pass_loses_the_fence_cas_and_adopts_nothing(isolated_db, monkeypatch):
    """Exactly one winner (NFR-2): the snapshot both passes read carries the OLD fence;
    the second ``declare_dead`` finds it bumped and returns None → 0."""
    orch = _primary(monkeypatch)
    try:
        await host_heartbeat.register("w1")
        await host_heartbeat.register("w2")
        await _age("w1", 999)
        await _host_row("w2", cap=6)
        await _session("a", host="w1")
        await _session("b", host="w1")
        hb = (await host_heartbeat.snapshot()).hosts["w1"]
        assert await adoption.adopt_host(orch, hb, dead_after_s=DEAD_AFTER) == 2
        assert await adoption.adopt_host(orch, hb, dead_after_s=DEAD_AFTER) == 0
        assert len([e for e in await provenance.list_events("a")
                    if e["kind"] == provenance.INTERRUPTED]) == 1
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_fresh_beat_is_never_fenced(isolated_db, monkeypatch):
    """NFR-3: a host that beat again between the snapshot and the CAS is healthy — the
    staleness leg of ``declare_dead`` refuses, and nothing of its is touched."""
    orch = _primary(monkeypatch)
    try:
        fence = await host_heartbeat.register("w1")
        await _host_row("w2", cap=6)
        await _session("a", host="w1")
        hb = host_heartbeat.HeartbeatState(host="w1", beat_at=datetime.utcnow(), fence=fence,
                                           epoch=None, age_s=999.0)   # a stale READ…
        assert await adoption.adopt_host(orch, hb, dead_after_s=DEAD_AFTER) == 0  # …fresh ROW
        assert (await _row("a")).host == "w1"
        assert (await host_heartbeat.snapshot()).hosts["w1"].fence == fence
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_no_able_host_leaves_sessions_in_place_loudly(isolated_db, monkeypatch, caplog):
    orch = _primary(monkeypatch)
    try:
        await host_heartbeat.register("w1")
        await _age("w1", 999)
        await _host_row("w2", healthy=False)   # the only other host is down
        await _session("a", host="w1")
        hb = (await host_heartbeat.snapshot()).hosts["w1"]
        with caplog.at_level("WARNING"):
            assert await adoption.adopt_host(orch, hb, dead_after_s=DEAD_AFTER) == 0
        assert (await _row("a")).host == "w1"
        assert "no healthy host can take session a" in caplog.text
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_targets_need_a_fresh_heartbeat_not_just_a_probe(isolated_db):
    """A registry row the edge probe still calls healthy but whose own heartbeat is stale
    is not a target — handing a session to the next host to die is not adoption."""
    await host_heartbeat.register("w2")
    await host_heartbeat.register("w3")
    await _age("w3", 999)
    await _host_row("w2", cap=4)
    await _host_row("w3", cap=4)
    await _host_row("w4", cap=4)           # healthy per probe, no heartbeat row at all
    targets = await adoption.candidate_targets(dead_after_s=DEAD_AFTER, exclude=frozenset({"w1"}))
    by_name = {c.name: c for c in targets.candidates}
    assert by_name["w2"].healthy is True
    assert by_name["w3"].healthy is False and by_name["w4"].healthy is False


def _cand(name, active, cap=4, **kw):
    return HostCandidate(name=name, enabled=True, healthy=True, weight=100,
                         active_sessions=active, max_sessions=cap, load_avg=0.5,
                         mem_available_mb=8000, **kw)


def test_pick_spreads_by_headroom_within_one_pass():
    t = adoption._Targets(candidates=[_cand("w2", 3), _cand("w3", 0)], taken={})
    picks = [t.pick(engine=None, platform=None) for _ in range(5)]
    # w3 has four free slots, w2 one: w3 takes the first four, then w2's single slot.
    assert sorted(picks) == ["w2", "w3", "w3", "w3", "w3"]
    assert t.pick(engine=None, platform=None) in ("w2", "w3")   # all full → still placed (FR-205)


def test_pick_returns_none_when_no_host_is_up():
    t = adoption._Targets(candidates=[HostCandidate(
        name="w2", enabled=True, healthy=False, weight=100, active_sessions=0,
        max_sessions=4, load_avg=None, mem_available_mb=None)], taken={})
    assert t.pick(engine=None, platform=None) is None


# ── the pass's authority gate (FR-201) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pass_runs_only_on_a_lease_holding_registered_primary(isolated_db, monkeypatch):
    from server.sessions import primary_lease
    orch = _primary(monkeypatch)
    try:
        monkeypatch.setattr(orchestrator_module, "daemon_role", lambda: "worker")
        assert await orch._adoption_authority() is False           # a worker never adopts
        monkeypatch.setattr(orchestrator_module, "daemon_role", lambda: "primary")
        orch._hb_fence = 1
        assert await orch._adoption_authority() is False           # no lease held
        orch._lease_fence = await primary_lease.acquire("w2", ttl_s=300.0)
        assert await orch._adoption_authority() is True
        orch._hb_fence = None                                      # own heartbeat not registered
        assert await orch._adoption_authority() is False
        orch._hb_fence = 1
        orch._hb_fenced = True                                     # fenced ourselves
        assert await orch._adoption_authority() is False
        orch._hb_fenced = False
        assert await orch._adoption_authority() is True
        orch._lease_fence = 999                                    # a stale incarnation's fence
        assert await orch._adoption_authority() is False
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_pass_adopts_every_dead_host_it_finds(isolated_db, monkeypatch):
    from server.sessions import primary_lease
    orch = _primary(monkeypatch)
    try:
        orch._hb_fence = await host_heartbeat.register("w2")
        orch._lease_fence = await primary_lease.acquire("w2", ttl_s=300.0)
        await _host_row("w2", cap=6)
        for h in ("w1", "w3"):
            await host_heartbeat.register(h)
            await _age(h, 999)
        await _session("a", host="w1")
        await _session("b", host="w3")
        assert await orch._adoption_pass_once() == 2
        assert (await _row("a")).host == "w2" and (await _row("b")).host == "w2"
        assert await orch._adoption_pass_once() == 0      # nothing left to adopt
    finally:
        await orch.stop()


def test_adoption_loop_is_primary_only_and_reaped_by_stop():
    """The loop task is in the fleet-global registry (test_primary_lease pins start()
    against it) and in stop()'s reap list (an orphaned loop outlives its app, #405)."""
    import inspect
    assert "_adoption_task" in orchestrator_module._PRIMARY_ONLY_LOOP_ATTRS
    assert '"_adoption_task"' in inspect.getsource(SessionOrchestrator.stop)
