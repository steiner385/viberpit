"""The runner's three read seams overlay a live capacity-governor plan onto
product policy / fleet config (phase 2, spec 2026-09-19 §6.3, D3, D5, §6.4).

No plan row present must be a no-op for every EXISTING policy/queue/dispatch
behaviour — the companion suites (``test_backlog_policy_seams.py`` etc.) cover
that unchanged. These tests drive the overlay itself: live vs stale vs
disabled, ``gather_product_coverage``, ``policies_by_slug``, the fleet
ceiling read (``_backlog_read_config``), and admission end to end
(``gather_implementation_snapshot(...).decide(...)``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from server.automations.capacity_store import write_plan
from server.automations.product_admission import gather_implementation_snapshot, gather_product_coverage
from server.automations.product_policy import policies_by_slug
from server.db import db_session
from server.db.models import AutomationTargetState, CapacityPlanRow, Project, ProjectRepo

# Reuse the orchestrator/runner-row helpers already built for the policy seams
# suite rather than growing a second "build an orchestrator" / "set the runner
# row" implementation.
from server.tests.test_backlog_policy_seams import _init, _orch_with_ctx, _set_runner_row


def _plan(per_product: dict, *, fleet: int = 10, applied: bool = True) -> dict:
    return {"pushLevel": "steady", "applied": applied, "fleetConcurrency": fleet,
            "perProduct": per_product, "pending": {}, "budget": {}, "binding": None,
            "reasons": [], "defects": []}


async def _seed(slug: str, repo: str, *, policy: dict) -> None:
    async with db_session() as db:
        project = Project(slug=slug, name=slug, automation_policy=policy)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        await db.flush()


def _identity(monkeypatch) -> None:
    async def resolve(names):
        return {name: name for name in names}
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", resolve)


async def _backdate_plan(minutes: int) -> None:
    async with db_session() as db:
        row = await db.get(CapacityPlanRow, "current")
        assert row is not None
        row.computed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)


# --------------------------------------------------------------------------- #
# gather_product_coverage: overlay the auto product, leave the pinned one     #
# --------------------------------------------------------------------------- #

def test_coverage_overlays_auto_product_and_leaves_pinned_alone(tmp_path):
    async def run():
        await _init(tmp_path, "seams_coverage.db")
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await _seed("pinned-product", "o/pinned",
                    policy={"dispatch": "auto", "capacity_mode": "pinned", "max_in_flight": 5})
        await write_plan(_plan({"auto-product": 3, "pinned-product": 9}))

        coverage = await gather_product_coverage()
        assert coverage.policies["auto-product"]["max_in_flight"] == 3
        assert coverage.policies["pinned-product"]["max_in_flight"] == 5

    asyncio.run(run())


def test_coverage_stale_plan_is_not_applied(tmp_path):
    async def run():
        await _init(tmp_path, "seams_stale.db")
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await write_plan(_plan({"auto-product": 3}))
        await _backdate_plan(11)

        coverage = await gather_product_coverage()
        assert coverage.policies["auto-product"]["max_in_flight"] == 1

    asyncio.run(run())


def test_coverage_disabled_governor_is_not_applied(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "seams_disabled.db")
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await write_plan(_plan({"auto-product": 3}))
        monkeypatch.setenv("VIBERPIT_CAPACITY_GOVERNOR_ENABLED", "false")

        coverage = await gather_product_coverage()
        assert coverage.policies["auto-product"]["max_in_flight"] == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# policies_by_slug (the sync queue-loader seam) agrees                       #
# --------------------------------------------------------------------------- #

def test_policies_by_slug_agrees_with_coverage(tmp_path):
    async def run():
        await _init(tmp_path, "seams_slug.db")
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await _seed("pinned-product", "o/pinned",
                    policy={"dispatch": "auto", "capacity_mode": "pinned", "max_in_flight": 5})
        await write_plan(_plan({"auto-product": 3, "pinned-product": 9}))

        async with db_session() as db:
            policies = await db.run_sync(policies_by_slug)
        assert policies["auto-product"]["max_in_flight"] == 3
        assert policies["pinned-product"]["max_in_flight"] == 5

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# _backlog_read_config: the fleet ceiling caps, never raises, the operator's  #
# own value                                                                   #
# --------------------------------------------------------------------------- #

def test_backlog_read_config_caps_max_concurrent_at_fleet_plan(tmp_path):
    async def run():
        await _init(tmp_path, "seams_fleet.db")
        orch = _orch_with_ctx()
        await _set_runner_row(mode="on", config={"max_concurrent": 6})

        await write_plan(_plan({}, fleet=4))
        mode, config = await orch._backlog_read_config()
        assert mode == "on"
        assert config["max_concurrent"] == 4  # min(stored=6, plan=4)

        await write_plan(_plan({}, fleet=10))
        mode, config = await orch._backlog_read_config()
        assert config["max_concurrent"] == 6  # min(stored=6, plan=10): stored wins

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Admission end to end: the plan's per-product cap governs product_capacity   #
# --------------------------------------------------------------------------- #

def test_admission_end_to_end_respects_plan_product_capacity(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "seams_admission.db")
        _identity(monkeypatch)
        await _seed("auto-product", "o/r", policy={"dispatch": "auto", "max_in_flight": 1})
        await _set_runner_row(mode="on", config={"max_concurrent": 5, "max_spawns_per_day": 10})
        async with db_session() as db:
            db.add(AutomationTargetState(automation_id="backlog-runner", target="o/r#1",
                                          state={"product": "auto-product"}))
            await db.flush()

        await write_plan(_plan({"auto-product": 2}))
        snapshot = await gather_implementation_snapshot(["o/r"], requested_product="auto-product")
        decision, _ = snapshot.decide(
            "o/r", requested_product="auto-product", trigger="automatic", effect="dispatch")
        assert decision.permitted, decision.constraints
        assert all(c.code != "product_capacity_full" for c in decision.constraints)

        await write_plan(_plan({"auto-product": 1}))
        snapshot = await gather_implementation_snapshot(["o/r"], requested_product="auto-product")
        decision, _ = snapshot.decide(
            "o/r", requested_product="auto-product", trigger="automatic", effect="dispatch")
        assert not decision.permitted
        assert "product_capacity_full" in {c.code for c in decision.constraints}

    asyncio.run(run())
