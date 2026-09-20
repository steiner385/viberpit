"""One runner-config read + plan overlay (spec 2026-09-20 D11): the orchestrator's
gate and admission's snapshot must see the same overlaid ceiling."""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.usefixtures("isolated_db")


async def _seed(max_concurrent: int, plan_fleet: int | None) -> None:
    from server.automations.capacity_store import write_plan
    from server.db import db_session
    from server.db.models import Automation as AutomationRow

    async with db_session() as db:
        db.add(AutomationRow(id="backlog-runner", enabled=True, mode="on",
                             config={"max_concurrent": max_concurrent}))
        await db.flush()
    if plan_fleet is not None:
        await write_plan({"pushLevel": "steady", "applied": True, "fleetConcurrency": plan_fleet,
                          "perProduct": {}, "pending": {}, "budget": {}, "binding": None,
                          "reasons": [], "defects": []})


def test_overlay_caps_the_stored_ceiling_at_the_live_plan():
    from server.automations.runner_config import read_runner_config

    async def run():
        await _seed(6, plan_fleet=3)
        mode, config = await read_runner_config()
        assert mode == "on"
        assert config["max_concurrent"] == 3
        _mode, stored = await read_runner_config(apply_plan=False)
        assert stored["max_concurrent"] == 6

    asyncio.run(run())


def test_a_given_plan_is_used_instead_of_a_fresh_read():
    """`plan=` is what a prior live_plan() returned; the seam applies it as-is
    and does NOT read the store again (which would see fleetConcurrency 3)."""
    from datetime import datetime, timezone

    from server.automations.runner_config import read_runner_config

    async def run():
        await _seed(6, plan_fleet=3)
        given = {"applied": True, "fleetConcurrency": 2,
                 "computedAt": datetime.now(timezone.utc).isoformat()}
        _mode, config = await read_runner_config(plan=given)
        assert config["max_concurrent"] == 2
        _mode, config = await read_runner_config(plan=None)
        assert config["max_concurrent"] == 6, "plan=None means no live plan: stored ceiling"

    asyncio.run(run())


def test_orchestrator_and_admission_read_through_the_same_seam(monkeypatch):
    import server.automations.product_admission as admission
    import server.sessions.orchestrator as orch_mod
    from server.automations import runner_config

    calls: list[dict] = []

    async def fake(*, plan=runner_config.UNSET, apply_plan=True):
        calls.append({"plan_given": plan is not runner_config.UNSET, "apply_plan": apply_plan})
        return "on", {"max_concurrent": 4, "enabled_repos": []}

    monkeypatch.setattr(runner_config, "read_runner_config", fake)

    async def run():
        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        mode, config = await orch._backlog_read_config()
        assert (mode, config["max_concurrent"]) == ("on", 4)
        await orch._backlog_read_config(apply_plan=False)
        snapshot = await admission.gather_implementation_snapshot([])
        assert snapshot.config["max_concurrent"] == 4

    asyncio.run(run())
    assert calls[0] == {"plan_given": False, "apply_plan": True}
    assert calls[1] == {"plan_given": False, "apply_plan": False}
    assert calls[2]["plan_given"] is True
