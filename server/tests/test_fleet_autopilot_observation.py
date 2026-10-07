"""Autopilot runtime evidence spans all session-owning hosts."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.automations.base import AutomationStatus
from server.sessions.fleet_orchestrator import FleetOrchestrator


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp()


def observed(*, age=0, advancing=0, escalated=0, waiting=0, **extra):
    return AutomationStatus(
        text="host observation", level="active" if advancing else "idle",
        counts={"advancing": advancing, "escalated": escalated, "waiting": waiting, **extra},
        observed_at=datetime.fromtimestamp(NOW - age, timezone.utc).isoformat(),
        waiting_reason="human" if escalated else None).to_dict()


def fleet_for(monkeypatch, primary_status, worker_status, *, reachable=None):
    monkeypatch.setattr("server.sessions.fleet_orchestrator.time.time", lambda: NOW)
    statuses = {"autopilot": primary_status,
                "pr-doctor": {"text": "primary only", "counts": {"active": 3}}}
    overview = {"statuses": statuses, "actionableCounts": {"pr-doctor": 7},
                "maintenanceCounts": {"autopilot": 2}}
    primary = SimpleNamespace(automation_statuses=AsyncMock(return_value=statuses),
                              automation_overview=AsyncMock(return_value=overview))
    worker = SimpleNamespace(
        automation_statuses=AsyncMock(return_value={"autopilot": worker_status,
                                                    "pr-doctor": {"counts": {"active": 99}}}),
        automation_overview=AsyncMock(side_effect=AssertionError("counts are primary-only")))
    hosts = {"primary": primary, "worker": worker}
    router = SimpleNamespace(names=lambda: list(hosts), get=hosts.__getitem__,
                             reachable_names=AsyncMock(return_value=reachable or list(hosts)))
    fleet = FleetOrchestrator(router, "primary")
    monkeypatch.setattr(fleet, "_log_unreachable", AsyncMock())
    return fleet, primary, worker, overview


@pytest.mark.asyncio
async def test_overview_combines_worker_evidence_without_mutating_primary(monkeypatch):
    fleet, primary, worker, original = fleet_for(
        monkeypatch, observed(age=10, advancing=1, waiting=2, deferring=2),
        observed(age=20, advancing=2, escalated=1, waiting=3, paused=3,
                 text_field="ignored", bool_field=True, nonfinite=float("nan")))
    result = await fleet.automation_overview()
    status = result["statuses"]["autopilot"]
    assert status["counts"] == {"advancing": 3, "escalated": 1, "waiting": 5,
                                 "deferring": 2, "paused": 3}
    assert status["text"] == "3 advancing, 1 escalated, 5 waiting"
    assert status["waitingReason"] == "human"
    assert status["observedAt"] == observed(age=20)["observedAt"]
    assert result["statuses"]["pr-doctor"] == original["statuses"]["pr-doctor"]
    assert result["actionableCounts"] == original["actionableCounts"]
    assert result["maintenanceCounts"] == original["maintenanceCounts"]
    assert original["statuses"]["autopilot"]["counts"]["advancing"] == 1
    primary.automation_overview.assert_awaited_once()
    primary.automation_statuses.assert_not_awaited()
    worker.automation_statuses.assert_awaited_once()
    worker.automation_overview.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_activity_and_repeated_polling_keep_producer_time(monkeypatch):
    fleet, _, _, _ = fleet_for(monkeypatch, observed(age=30), observed(age=40))
    first = await fleet.automation_statuses()
    second = await fleet.automation_statuses()
    assert first == second
    assert first["autopilot"]["level"] == "idle"
    assert first["autopilot"]["observedAt"] == observed(age=40)["observedAt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_status", [
    None, {}, observed(age=901, advancing=20),
    {**observed(advancing=20), "observedAt": "bad date"},
    {**observed(advancing=20), "observedAt": None},
    {**observed(advancing=20), "lastRefreshFailedAt": observed()["observedAt"]},
])
async def test_missing_stale_or_failed_worker_is_unavailable(monkeypatch, worker_status):
    fleet, _, _, _ = fleet_for(monkeypatch, observed(advancing=1), worker_status)
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["level"] == "unknown"
    assert "unavailable" in status["text"]
    assert "worker" in status["text"]
    assert status["counts"]["advancing"] == 1
    assert status["counts"]["hosts_unavailable"] == 1


@pytest.mark.asyncio
async def test_stale_human_waiting_evidence_is_retained(monkeypatch):
    fleet, _, _, _ = fleet_for(monkeypatch, observed(), observed(age=901, escalated=1))
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["waitingReason"] == "human"
    assert status["observedAt"] == observed(age=901)["observedAt"]
    assert status["counts"]["escalated"] == 0


@pytest.mark.asyncio
async def test_staleness_boundary_is_inclusive(monkeypatch):
    fleet, _, _, _ = fleet_for(monkeypatch, observed(), observed(age=900, advancing=2))
    assert (await fleet.automation_statuses())["autopilot"]["counts"]["advancing"] == 2


@pytest.mark.asyncio
async def test_unreachable_registered_worker_is_not_polled_or_silently_dropped(monkeypatch):
    fleet, _, worker, _ = fleet_for(
        monkeypatch, observed(), observed(advancing=9), reachable=["primary"])
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["level"] == "unknown"
    assert status["counts"]["hosts_unavailable"] == 1
    worker.automation_statuses.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_timeout_is_bounded_and_reported(monkeypatch):
    fleet, _, worker, _ = fleet_for(monkeypatch, observed(), observed())
    monkeypatch.setattr("server.sessions.fleet_orchestrator.AUTOPILOT_STATUS_TIMEOUT_S", 0.01)

    async def blocked():
        await asyncio.Event().wait()

    worker.automation_statuses.side_effect = blocked
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["level"] == "unknown"
    fleet._log_unreachable.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["automation_statuses", "automation_overview"])
async def test_primary_failure_propagates(monkeypatch, method):
    fleet, primary, _, _ = fleet_for(monkeypatch, observed(), observed())
    getattr(primary, method).side_effect = OSError("primary unavailable")
    with pytest.raises(OSError, match="primary unavailable"):
        await getattr(fleet, method)()


@pytest.mark.asyncio
async def test_worker_status_rpcs_run_in_parallel(monkeypatch):
    fleet, primary, _, _ = fleet_for(monkeypatch, observed(), observed())
    started = set()
    both_started = asyncio.Event()

    async def worker_status(name):
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        return {"autopilot": observed(advancing=1)}

    hosts = {"primary": primary, **{
        name: SimpleNamespace(automation_statuses=lambda n=name: worker_status(n))
        for name in ("worker-a", "worker-b")}}
    fleet._router = SimpleNamespace(names=lambda: list(hosts), get=hosts.__getitem__,
                                    reachable_names=AsyncMock(return_value=list(hosts)))
    async with asyncio.timeout(1):
        statuses = await fleet.automation_statuses()
    assert statuses["autopilot"]["counts"]["advancing"] == 2


@pytest.mark.asyncio
async def test_stale_primary_counts_are_not_added_to_fresh_worker(monkeypatch):
    fleet, _, _, _ = fleet_for(
        monkeypatch, observed(age=901, advancing=20), observed(advancing=2))
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["counts"]["advancing"] == 2
    assert status["level"] == "unknown"
    assert "primary" in status["text"]


def test_shared_summary_renders_explicit_waiting_total_only():
    from server.automations.autopilot_observation import autopilot_status

    assert autopilot_status(None).level == "unknown"
    status = autopilot_status({"advancing": 0, "escalated": 0,
                               "waiting": 2, "deferring": 2})
    assert status.text == "0 advancing, 2 waiting"
    assert autopilot_status({"advancing": 0, "escalated": 1}).waiting_reason == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("advancing", [0, 2])
async def test_partial_producer_warning_survives_fleet_aggregation(monkeypatch, advancing):
    from server.automations.context import observed_status
    from server.automations.autopilot_observation import autopilot_status

    producer = SimpleNamespace(
        observed_at=observed(age=10)["observedAt"], observation_degraded=True)
    partial = observed_status(producer, autopilot_status({
        "advancing": advancing, "escalated": 0, "waiting": 0})).to_dict()
    fleet, _, _, _ = fleet_for(monkeypatch, observed(), partial)
    status = (await fleet.automation_statuses())["autopilot"]
    assert status["level"] == "warn"
    assert "observations need attention" in status["text"]
    assert "worker" in status["text"]
    assert status["observedAt"] == producer.observed_at
    assert status["counts"]["advancing"] == advancing
