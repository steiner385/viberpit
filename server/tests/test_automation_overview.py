"""Observed status and bounded actionable counts share the list contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest

from server.automations.base import ActionableItem
from server.sessions.orchestrator import SessionOrchestrator


def test_missing_observation_is_unknown_but_observed_zero_is_idle():
    orch = object.__new__(SessionOrchestrator)
    orch._pr_doctor_ctx = SimpleNamespace()
    assert orch.automation_statuses()["pr-doctor"]["level"] == "unknown"
    orch._pr_doctor_ctx.pr_doctor_counts = {"failing": 0}
    assert orch.automation_statuses()["pr-doctor"]["level"] == "idle"
    assert orch.automation_statuses()["sdlc-synthesizer"]["level"] == "unknown"


def test_previously_unwired_contexts_render_observations():
    orch = object.__new__(SessionOrchestrator)
    orch._sdlc_intake_ctx = SimpleNamespace(sdlc_intake_counts={"pending": 2, "awaiting": 1})
    orch._control_band_ctx = SimpleNamespace(control_band_counts={"watched": 2, "breaching": 1})
    orch._archaudit_ctx = SimpleNamespace(archive_audit_counts={"total": 3, "unarchived": 0})
    statuses = orch.automation_statuses()
    for key in ("sdlc-intake", "control-band-monitor", "archive-audit"):
        assert statuses[key]["counts"] is not None
        assert statuses[key]["level"] != "unknown"


@pytest.mark.asyncio
async def test_counts_use_manual_provider_items_and_isolate_failure():
    orch = object.__new__(SessionOrchestrator)
    good = SimpleNamespace(actionable=AsyncMock(return_value=[ActionableItem("a", "one")]))
    bad = SimpleNamespace(actionable=AsyncMock(side_effect=RuntimeError("private detail")))
    orch._action_providers = {"pr-doctor": good, "backlog-runner": bad}
    first, second = await asyncio.gather(orch.automation_overview(), orch.automation_overview())
    assert first["actionableCounts"]["pr-doctor"] == 1
    assert first["actionableCounts"]["backlog-runner"] is None
    assert first["actionableCounts"]["pm-sync"] == 0
    assert second["actionableCounts"] == first["actionableCounts"]
    good.actionable.assert_awaited_once()
    bad.actionable.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_provider_is_unknown_without_blocking_other_counts(monkeypatch):
    import server.sessions.orchestrator as module
    monkeypatch.setattr(module, "AUTOMATION_ACTIONABLE_TIMEOUT_S", 0.01)
    async def slow():
        await asyncio.Event().wait()
    orch = object.__new__(SessionOrchestrator)
    orch._action_providers = {"pr-doctor": SimpleNamespace(actionable=slow)}
    snapshot = await asyncio.wait_for(orch.automation_overview(), timeout=1)
    assert snapshot["actionableCounts"]["pr-doctor"] is None
    assert snapshot["actionableCounts"]["pm-sync"] == 0


def test_render_failure_isolated_and_not_retried_with_fake_idle(monkeypatch):
    from server.automations.registry import registry
    automation = registry.get("pr-doctor")
    calls = []
    def broken(ctx):
        calls.append(ctx)
        raise RuntimeError("private detail")
    monkeypatch.setattr(automation, "status", broken)
    orch = object.__new__(SessionOrchestrator)
    orch._pr_doctor_ctx = SimpleNamespace(pr_doctor_counts={"failing": 1})
    orch._autopilot_ctx = SimpleNamespace(autopilot_counts={"advancing": 1})
    statuses = orch.automation_statuses()
    assert len(calls) == 1
    assert statuses["pr-doctor"]["level"] == "unknown"
    assert "private detail" not in statuses["pr-doctor"]["text"]
    assert statuses["autopilot"]["level"] == "active"


@pytest.mark.asyncio
async def test_count_cache_expires_and_retries_unknown_provider(monkeypatch):
    import server.sessions.orchestrator as module
    monkeypatch.setattr(module, "AUTOMATION_ACTIONABLE_CACHE_S", 0)
    orch = object.__new__(SessionOrchestrator)
    provider = SimpleNamespace(actionable=AsyncMock(side_effect=[RuntimeError(), []]))
    orch._action_providers = {"pr-doctor": provider}
    assert (await orch.automation_overview())["actionableCounts"]["pr-doctor"] is None
    assert (await orch.automation_overview())["actionableCounts"]["pr-doctor"] == 0


def test_observed_schedule_wait_is_separate_from_activity():
    orch = object.__new__(SessionOrchestrator)
    orch._schedule_blocked = {"pr-doctor": True}
    orch._pr_doctor_ctx = SimpleNamespace(pr_doctor_counts={"failing": 2})
    status = orch.automation_statuses()["pr-doctor"]
    assert status["waitingReason"] == "schedule"
    assert "outside active hours" in status["text"]
    assert status["counts"] == {"failing": 2}
    assert status["level"] == "active"


def test_invoked_tool_explains_no_loop_and_error_without_counts_is_preserved(monkeypatch):
    from server.automations.base import AutomationStatus
    from server.automations.registry import registry
    orch = object.__new__(SessionOrchestrator)
    orch._pr_doctor_ctx = SimpleNamespace()
    monkeypatch.setattr(registry.get("pr-doctor"), "status",
                        lambda ctx: AutomationStatus("Observed failure", level="error"))
    statuses = orch.automation_statuses()
    assert statuses["pr-doctor"]["level"] == "error"
    assert statuses["pr-doctor"]["text"] == "Observed failure"
    assert statuses["pm-classify"]["text"] == "Invoked tool; no continuous loop"


@pytest.mark.asyncio
async def test_overview_crosses_rpc_without_converting_unknown_count_to_zero():
    from fastapi import FastAPI
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.remote import RemoteOrchestrator
    expected = {"statuses": {"pr-doctor": {"level": "unknown"}},
                "actionableCounts": {"pr-doctor": None}}
    orch = SimpleNamespace(automation_overview=AsyncMock(return_value=expected))
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, orch)
    assert await app.state.rpc["automation_overview"]({}) == expected
    assert await InProcessOrchestratorAdapter(orch).automation_overview() == expected
    client = SimpleNamespace(call=AsyncMock(return_value=expected))
    remote = object.__new__(RemoteOrchestrator)
    remote._client = client
    assert await remote.automation_overview() == expected
    client.call.assert_awaited_once_with("automation_overview")


@pytest.mark.asyncio
async def test_removed_schedule_clears_observed_wait(monkeypatch):
    from contextlib import asynccontextmanager
    import server.sessions.orchestrator as module
    result = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(schedule=None))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(module, "db_session", session)
    orch = object.__new__(SessionOrchestrator)
    orch._schedule_blocked = {"pr-doctor": True}
    assert await orch._schedule_allows_tick("pr-doctor")
    assert "pr-doctor" not in orch._schedule_blocked


@pytest.mark.asyncio
async def test_trigger_invalidates_count_snapshot():
    from server.automations.base import TriggerResult
    orch = object.__new__(SessionOrchestrator)
    provider = SimpleNamespace(
        actionable=AsyncMock(side_effect=[[ActionableItem("a", "one")], []]),
        valid_kinds={"fix"}, trigger=AsyncMock(return_value=TriggerResult("done", "ok", "Done")))
    orch._action_providers = {"pr-doctor": provider}
    assert (await orch.automation_overview())["actionableCounts"]["pr-doctor"] == 1
    await orch.automation_trigger("pr-doctor", "a", "fix")
    assert (await orch.automation_overview())["actionableCounts"]["pr-doctor"] == 0


def test_schedule_pause_preserves_synthesizer_human_attention():
    orch = object.__new__(SessionOrchestrator)
    orch._sdlc_synth_ctx = SimpleNamespace(
        sdlc_synthesizer_counts={"awaiting": 1, "proposed": 0})
    before = orch.automation_statuses()["sdlc-synthesizer"]
    assert before["waitingReason"] == "human"
    orch._schedule_blocked = {"sdlc-synthesizer": True}
    paused = orch.automation_statuses()["sdlc-synthesizer"]
    # Rows/navigation must retain human attention even with no live session or
    # action-provider item. The schedule only prevents new scheduled work.
    assert paused["waitingReason"] == "human"
    assert "outside active hours" in paused["text"]
    assert "1 awaiting human action" in paused["text"]
    assert paused["counts"] == before["counts"]
    assert paused["level"] == before["level"]
