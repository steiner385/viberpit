"""Fleet continuity drill (spec 002 acceptance scenarios 1 and 2, #949 Task 20).

Two orchestrators share one SQLite DB and stand in for two hosts: ``w1`` (a worker that
"dies" — its heartbeat simply stops) and ``w2`` (the primary). Host identity is
process-global (``self_host_name`` / ``daemon_role`` read the environment), so the
drill flips a module-level switch before driving each orchestrator; every call site
reads the orchestrator module's globals at call time, which is what makes that work.

Scenario 1 — dead-host adoption: w1's beat ages past the threshold with an
``in_progress`` session; w2's pass fences w1 and adopts the session (restamped w2,
``interrupted{host_lost}`` + ``moved_host`` events, status idle); w2's pickup resumes
it from its transcript and nudges it.

Scenario 2 — the partitioned host returns: w1 beats, is refused, and stands down —
its fake driver is closed as a restart, it writes no status, and it reloads nothing it
no longer owns (NFR-2: no session is driven by two hosts).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import Host, HostHeartbeat, Session as DbSession
from server.sessions import host_heartbeat, primary_lease, provenance
from server.sessions import orchestrator as orchestrator_module
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.orphaned_asks import CLOSE_REASON_RESTART
from server.sessions.types import SYSTEM_IDENTITY
from server.tests.fakes.fake_factory import fake_sdk_factory

DEAD_AFTER = 300.0


class _Identity:
    """Which host the code under test is 'on' right now."""
    host = "w1"
    role = "worker"


class _Driver:
    def __init__(self):
        self.closed_with: list = []

    async def aclose(self, reason=None):
        self.closed_with.append(reason)


class _Host:
    def __init__(self, name: str, role: str):
        self.name, self.role = name, role
        self.orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        self.calls: dict = {"ensure": [], "prompt": []}
        orch = self.orch

        async def ensure_driver(sid, identity=SYSTEM_IDENTITY):
            self.calls["ensure"].append((sid, orch._records[sid].engine_session_id))
            orch._drivers[sid] = _Driver()
            return orch._drivers[sid]

        async def send_prompt(sid, text, attachment_ids=None, *, replayable=None, **_kw):
            self.calls["prompt"].append((sid, text))
            return "sent"

        orch.ensure_driver = ensure_driver            # type: ignore[assignment,method-assign]
        orch.send_prompt = send_prompt                # type: ignore[assignment,method-assign]
        orch._find_transcript = lambda cfg, csid, cwd=None: f"{cfg}/projects/p/{csid}.jsonl"  # type: ignore[assignment,method-assign]

    def become(self) -> None:
        _Identity.host, _Identity.role = self.name, self.role


async def _age(host: str, seconds: float) -> None:
    async with db_session() as db:
        await db.execute(sa_update(HostHeartbeat).where(HostHeartbeat.host == host)
                         .values(beat_at=datetime.utcnow() - timedelta(seconds=seconds)))


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalar_one()


@pytest.mark.asyncio
async def test_fleet_continuity_drill(isolated_db, monkeypatch):
    monkeypatch.setattr(orchestrator_module, "self_host_name", lambda: _Identity.host)
    monkeypatch.setattr(orchestrator_module, "daemon_role", lambda: _Identity.role)
    monkeypatch.setattr(orchestrator_module, "host_dead_after_s", lambda: DEAD_AFTER)
    async with db_session() as db:
        db.add(Host(name="w1", endpoint="https://w1:8443", enabled=True, healthy=True, max_sessions=4))
        db.add(Host(name="w2", endpoint="https://w2:8443", enabled=True, healthy=True, max_sessions=4))
        db.add(DbSession(id="s", name="ship #949", account_name="acct", engine="claude_code",
                         engine_session_id="c-s", status="in_progress", cwd="/tmp/s",
                         repo="o/r", branch="feat/949", host="w1"))
    w1, w2 = _Host("w1", "worker"), _Host("w2", "primary")
    try:
        # ── boot both hosts ──
        w1.become()
        w1.orch._hb_fence = await host_heartbeat.register("w1")
        await w1.orch.rehydrate()
        assert set(w1.orch._records) == {"s"}
        live_on_w1 = _Driver()
        w1.orch._drivers["s"] = live_on_w1
        w2.become()
        w2.orch._hb_fence = await host_heartbeat.register("w2")
        w2.orch._lease_fence = await primary_lease.acquire("w2", ttl_s=300.0)
        await w2.orch.rehydrate()                       # the primary mirrors every row
        assert w2.orch._records["s"].host == "w1"

        # ── a healthy fleet adopts nothing ──
        assert await w2.orch._adoption_pass_once() == 0

        # ── scenario 1: w1 goes silent past the threshold ──
        await _age("w1", DEAD_AFTER + 60)
        assert await w2.orch._adoption_pass_once() == 1
        row = await _row("s")
        assert (row.host, row.status, row.adopted_from, row.moved_from) == ("w2", "idle", "w1", "w1")
        assert (await host_heartbeat.snapshot()).hosts["w1"].fence == 2
        assert w2.orch._records["s"].host == "w2" and w2.orch._records["s"].status == "idle"

        assert await w2.orch._pickup_adopted_once() == 1
        assert w2.calls["ensure"] == [("s", "c-s")]           # resumed from its transcript
        assert "moved from w1 to w2" in w2.calls["prompt"][0][1]
        kinds = [(e["kind"], e["detail"].get("kind") or e["detail"].get("resume"))
                 for e in await provenance.list_events("s")]
        assert kinds == [(provenance.MOVED_HOST, "transcript"), (provenance.INTERRUPTED, "host_lost")]
        assert (await _row("s")).adopted_from is None
        assert await w2.orch._adoption_pass_once() == 0       # nothing left on w1

        # ── scenario 2: w1 comes back and beats ──
        w1.become()
        writes: list = []
        real_update = orchestrator_module.SessionOrchestrator._update_session_row

        async def spy(self, sid, values):
            writes.append((sid, values))
            return await real_update(self, sid, values)
        monkeypatch.setattr(orchestrator_module.SessionOrchestrator, "_update_session_row", spy)

        await w1.orch._heartbeat_tick()
        assert live_on_w1.closed_with == [CLOSE_REASON_RESTART]   # its driver is gone
        assert w1.orch._drivers == {} and w1.orch._records == {}  # it owns nothing now
        assert writes == []                                       # and persisted nothing
        assert w1.orch._hb_fence == 2 and w1.orch._hb_fenced is False   # back at the new fence
        assert (await _row("s")).host == "w2" and (await _row("s")).status == "idle"
        assert "s" in w2.orch._drivers                            # exactly one host drives it
    finally:
        w1.become()
        await w1.orch.stop()
        w2.become()
        await w2.orch.stop()
