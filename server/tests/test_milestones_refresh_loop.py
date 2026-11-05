"""The milestones refresh loop (spec 2026-11-05 §8): which ladders a 15 s tick
refreshes, the off / dry_run / on modes, one failing ladder not stopping the rest;
and its wiring (registered, primary-only, torn down)."""
from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone

from server.automations.context import AutomationContext
from server.automations.milestones_refresh import (
    MilestonesRefresh,
    MilestonesRefreshLoop,
)
from server.pm.milestone_refresh import STALE_AFTER_S, RefreshResult

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
NAIVE_NOW = NOW.replace(tzinfo=None)


class _Harness:
    def __init__(self, *, mode="on", fetched=None, requested=None, fail=()):
        self.mode = mode
        self.fetched = fetched or {}
        self.requested = requested or {}
        self.fail = set(fail)
        self.calls: list[tuple[str, bool]] = []
        self.now = NOW
        self.ctx = AutomationContext("milestones-refresh")
        self.loop = MilestonesRefreshLoop(
            self.ctx, read_config=self.read_config, list_ladder_projects=self.list_projects,
            list_requested=self.list_requested, refresh=self.refresh, now=lambda: self.now)

    async def read_config(self):
        return self.mode, {}

    async def list_projects(self):
        return list(self.fetched.items())

    async def list_requested(self):
        return dict(self.requested)

    async def refresh(self, project_id, *, dry_run):
        self.calls.append((project_id, dry_run))
        if project_id in self.fail:
            raise RuntimeError("gh graphql failed: boom")
        return RefreshResult(written=3, errors=0)


def test_loop_refreshes_each_ladder_and_survives_one_failure(caplog):
    h = _Harness(fetched={
        "never": None,                                                   # due: never fetched
        "broken": NAIVE_NOW - timedelta(seconds=STALE_AFTER_S + 1),      # due, and fails
        "stale": NAIVE_NOW - timedelta(seconds=STALE_AFTER_S + 5),       # due
        "fresh": NAIVE_NOW - timedelta(seconds=30),                      # not due
    }, fail={"broken"})
    with caplog.at_level(logging.INFO, logger="server.automations.milestones_refresh"):
        asyncio.run(h.loop.tick())
    assert sorted(h.calls) == [("broken", False), ("never", False), ("stale", False)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == ["milestones_refresh.ladder_failed project=broken"]
    assert h.ctx.milestones_refresh_counts == {"refreshed": 2, "failed": 1, "dryRun": 0}


def test_loop_off_does_nothing():
    h = _Harness(mode="off", fetched={"never": None}, requested={"never": NAIVE_NOW})
    asyncio.run(h.loop.tick())
    assert h.calls == []
    assert getattr(h.ctx, "milestones_refresh_counts", None) is None


def test_dry_run_refreshes_without_writing():
    h = _Harness(mode="dry_run", fetched={"never": None})
    asyncio.run(h.loop.tick())
    assert h.calls == [("never", True)]
    assert h.ctx.milestones_refresh_counts == {"refreshed": 0, "failed": 0, "dryRun": 1}


def test_request_newer_than_fetch_refreshes_a_fresh_ladder():
    fetched = NAIVE_NOW - timedelta(seconds=120)
    h = _Harness(fetched={"a": fetched, "b": fetched},
                 requested={"a": fetched + timedelta(seconds=60), "b": fetched - timedelta(seconds=1)})
    asyncio.run(h.loop.tick())
    assert h.calls == [("a", False)]


def test_failed_or_dry_run_ladder_waits_a_full_interval_before_retrying():
    h = _Harness(fetched={"broken": None}, fail={"broken"})
    asyncio.run(h.loop.tick())
    h.now = NOW + timedelta(seconds=15)
    asyncio.run(h.loop.tick())
    assert h.calls == [("broken", False)]
    # A request stamped after the failed attempt is honoured at once.
    h.requested = {"broken": (h.now + timedelta(seconds=1)).replace(tzinfo=None)}
    h.now = NOW + timedelta(seconds=30)
    asyncio.run(h.loop.tick())
    assert h.calls == [("broken", False), ("broken", False)]
    h.now = NOW + timedelta(seconds=30 + STALE_AFTER_S)
    asyncio.run(h.loop.tick())
    assert len(h.calls) == 3


def test_a_listing_failure_is_survived(caplog):
    h = _Harness(fetched={"a": None})

    async def boom():
        raise RuntimeError("db down")
    h.loop._list_ladder_projects = boom
    asyncio.run(h.loop.tick())
    assert h.calls == []
    assert h.ctx.last_refresh_failed_at is not None


# ---------------------------------------------------------------- wiring


def test_registered_with_oversight():
    from server.automations.registry import registry
    a = registry.get("milestones-refresh")
    assert isinstance(a, MilestonesRefresh)
    assert a.modes == ("off", "dry_run", "on") and a.work_class == "maintenance"
    oversight = registry.oversight_for("milestones-refresh")
    assert oversight is not None
    assert set(oversight.mode_descriptions) == {"off", "dry_run", "on"}
    assert {r.target_id for r in oversight.relationships} >= {"pm-sync"}


def test_loop_is_primary_only_started_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_milestones_refresh_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    assert '"_milestones_refresh_task"' in inspect.getsource(mod.SessionOrchestrator.stop)
    assert "milestones-refresh-loop" in inspect.getsource(mod.SessionOrchestrator.start)
    statuses_src = inspect.getsource(mod.SessionOrchestrator.automation_statuses)
    assert '"milestones-refresh": getattr(self, "_milestones_refresh_ctx", None)' in statuses_src


def test_orchestrator_refresh_seam_uses_partial_ok_graphql(monkeypatch):
    """The loop's refresh reaches GitHub through ``_pm_gh_graphql(partial_ok=True)``, so a
    bad ref errors only its alias (Review Focus 3)."""
    from server.pm import milestone_refresh
    from server.sessions.orchestrator import SessionOrchestrator
    seen: dict = {}

    async def fake_refresh(project_id, *, graphql, list_runs, now, dry_run):
        seen.update(project_id=project_id, dry_run=dry_run,
                    payload=await graphql("o", "query { x }"))
        return RefreshResult(0, 0)

    async def fake_gql(self, owner, query, *, partial_ok):
        return {"owner": owner, "query": query, "partial_ok": partial_ok}

    monkeypatch.setattr(milestone_refresh, "refresh_ladder", fake_refresh)
    monkeypatch.setattr(SessionOrchestrator, "_pm_gh_graphql", fake_gql)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    asyncio.run(SessionOrchestrator._milestones_refresh_run(orch, "p1", dry_run=True))
    assert seen == {"project_id": "p1", "dry_run": True,
                    "payload": {"owner": "o", "query": "query { x }", "partial_ok": True}}


def test_automatic_refresh_enabled_reads_the_automation_row(isolated_db):
    from server.automations.milestones_refresh import automatic_refresh_enabled
    from server.db import db_session
    from server.db.models import Automation

    async def run():
        assert await automatic_refresh_enabled() is False  # no row: off
        async with db_session() as db:
            db.add(Automation(id="milestones-refresh", enabled=False))
        assert await automatic_refresh_enabled() is False
        async with db_session() as db:
            row = await db.get(Automation, "milestones-refresh")
            row.enabled = True
        assert await automatic_refresh_enabled() is True  # enabled, no mode: on
        async with db_session() as db:
            row = await db.get(Automation, "milestones-refresh")
            row.mode = "dry_run"
        assert await automatic_refresh_enabled() is True
    asyncio.run(run())
