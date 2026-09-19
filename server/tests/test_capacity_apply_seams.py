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


def test_backlog_read_config_apply_plan_false_is_the_operators_own_ceiling(tmp_path):
    """F1, fix round 1: the governor's planner reads THIS seam (via
    ``capacity_wiring.sources_for``'s ``runner_config``) to learn the
    operator's stored ceiling, never a prior tick's already-overlaid output —
    feeding the overlay back in would ratchet ``fleetConcurrency`` down every
    tick with no way back up when an account resets or the operator raises
    the stored cap."""
    async def run():
        await _init(tmp_path, "seams_fleet_raw.db")
        orch = _orch_with_ctx()
        await _set_runner_row(mode="on", config={"max_concurrent": 6})
        await write_plan(_plan({}, fleet=3))

        _mode, overlaid = await orch._backlog_read_config()
        assert overlaid["max_concurrent"] == 3

        _mode, raw = await orch._backlog_read_config(apply_plan=False)
        assert raw["max_concurrent"] == 6

    asyncio.run(run())


def test_wiring_runner_config_feeds_the_planner_the_operators_own_ceiling(tmp_path):
    """Cheap wiring-level check that ``capacity_wiring.sources_for`` actually
    wires ``apply_plan=False`` onto ``runner_config`` — the seam test above
    proves the flag works, this proves the wiring uses it."""
    async def run():
        await _init(tmp_path, "seams_wiring.db")
        orch = _orch_with_ctx()
        await _set_runner_row(mode="on", config={"max_concurrent": 6})
        await write_plan(_plan({}, fleet=3))

        from server.automations.capacity_wiring import sources_for
        sources = sources_for(orch)
        _mode, config = await sources.runner_config()
        assert config["max_concurrent"] == 6  # the operator's own ceiling, not the plan's overlay

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


def test_admission_end_to_end_respects_plan_fleet_ceiling(tmp_path, monkeypatch):
    """F3, fix round 1: ``gather_implementation_snapshot`` used to read the
    runner config itself (unoverlaid), so ``decide()``'s ``global_cap`` was
    the stored ``max_concurrent`` while ``_refill`` enforced the plan's
    derived one — two different ceilings for the same tick."""
    async def run():
        await _init(tmp_path, "seams_admission_fleet.db")
        _identity(monkeypatch)
        await _seed("auto-product", "o/r", policy={"dispatch": "auto", "max_in_flight": 5})
        await _set_runner_row(mode="on", config={"max_concurrent": 6, "max_spawns_per_day": 10})
        async with db_session() as db:
            db.add(AutomationTargetState(automation_id="backlog-runner", target="o/r#1",
                                          state={"product": "auto-product"}))
            await db.flush()

        # fleet ceiling 1, product cap 5 (not itself binding): one in-flight
        # claim already fills the whole fleet ceiling.
        await write_plan(_plan({"auto-product": 5}, fleet=1))
        snapshot = await gather_implementation_snapshot(["o/r"], requested_product="auto-product")
        decision, _ = snapshot.decide(
            "o/r", requested_product="auto-product", trigger="automatic", effect="dispatch")
        assert not decision.permitted
        assert "global_capacity_full" in {c.code for c in decision.constraints}

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# live_plan / live_plan_sync never raise (F2, fix round 1)                    #
# --------------------------------------------------------------------------- #

def test_live_plan_never_raises_on_a_broken_read(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "seams_live_plan_raises.db")
        from server.automations import capacity_store

        async def boom():
            raise RuntimeError("db exploded")

        monkeypatch.setattr(capacity_store, "read_plan", boom)
        assert await capacity_store.live_plan() is None

    asyncio.run(run())


def test_live_plan_sync_never_raises_on_a_broken_read():
    from server.automations import capacity_store

    class BrokenSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("db exploded")

    assert capacity_store.live_plan_sync(BrokenSession()) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# load_policies (the async twin) overlays exactly like policies_by_slug (F5)  #
# --------------------------------------------------------------------------- #

def test_load_policies_agrees_with_policies_by_slug(tmp_path):
    async def run():
        await _init(tmp_path, "seams_load_policies.db")
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await write_plan(_plan({"auto-product": 3}))

        from server.automations.product_policy import load_policies
        async with db_session() as db:
            policies = await load_policies(db)
        assert policies["auto-product"]["max_in_flight"] == 3

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# applied=False (monitor mode): no overlay anywhere (F6, fix round 1)         #
# --------------------------------------------------------------------------- #

def test_monitor_mode_applied_false_overlays_nothing(tmp_path):
    async def run():
        await _init(tmp_path, "seams_monitor.db")
        orch = _orch_with_ctx()
        await _seed("auto-product", "o/auto", policy={"dispatch": "auto", "max_in_flight": 1})
        await _set_runner_row(mode="on", config={"max_concurrent": 6})
        await write_plan(_plan({"auto-product": 3}, fleet=2, applied=False))

        coverage = await gather_product_coverage()
        assert coverage.policies["auto-product"]["max_in_flight"] == 1

        async with db_session() as db:
            policies = await db.run_sync(policies_by_slug)
        assert policies["auto-product"]["max_in_flight"] == 1

        _mode, config = await orch._backlog_read_config()
        assert config["max_concurrent"] == 6

    asyncio.run(run())
