"""Observation freshness belongs to producer publication, never status polling."""
from types import SimpleNamespace

import pytest

from server.automations.base import AutomationStatus
from server.automations.context import observed_status, observation_failed, publish_observation


def test_observation_failure_preserves_timestamp_and_recovery_requires_publication():
    ctx = SimpleNamespace()
    publish_observation(ctx, "test_counts", {"open": 3})
    status = AutomationStatus("3 open", counts=ctx.test_counts,
                              products=[{"slug": "p"}], waiting_reason="schedule")
    first = observed_status(ctx, status).to_dict()
    assert first["observedAt"]
    assert observed_status(ctx, status).to_dict() == first
    observation_failed(ctx)
    failed = observed_status(ctx, status).to_dict()
    assert failed["observedAt"] == first["observedAt"]
    assert failed["level"] == "unknown"
    assert failed["counts"] is None and failed["products"] is None
    assert failed["lastRefreshFailedAt"]
    assert failed["waitingReason"] == "schedule"
    assert observed_status(ctx, status).to_dict() == failed
    publish_observation(ctx, "test_counts", {"open": 0})
    recovered = observed_status(ctx, AutomationStatus("0 open", counts=ctx.test_counts)).to_dict()
    assert recovered["counts"] == {"open": 0}
    assert "lastRefreshFailedAt" not in recovered
    assert recovered["observedAt"] >= first["observedAt"]


@pytest.mark.asyncio
async def test_archive_empty_refresh_then_failure_and_disabled_tick():
    from unittest.mock import AsyncMock
    from server.automations.archive_audit import ArchiveAuditLoop

    ctx = SimpleNamespace(emit=AsyncMock())
    config = AsyncMock(return_value=("act", {}))
    rows = AsyncMock(return_value=[])
    loop = ArchiveAuditLoop(ctx, read_config=config, list_rows=rows,
                            gh_fn=lambda *_: [], gh_num_fn=None,
                            unarchive_fn=AsyncMock(), clock=lambda: 1000000)
    await loop.tick()
    assert ctx.archive_audit_counts["total"] == 0
    first = ctx.observed_at
    rows.side_effect = RuntimeError("private backend failure")
    await loop.tick()
    failed = observed_status(ctx, AutomationStatus("idle")).to_dict()
    assert failed["level"] == "unknown"
    assert failed["observedAt"] == first
    assert "private" not in str(failed)
    config.return_value = ("off", {})
    await loop.tick()
    assert observed_status(ctx, AutomationStatus("idle")).to_dict() == failed
    rows.side_effect = None
    config.return_value = ("act", {})
    await loop.tick()
    assert ctx.last_refresh_failed_at is None


@pytest.mark.asyncio
async def test_backlog_all_reads_fail_preserves_success_partial_recovery_keeps_known_counts():
    from unittest.mock import AsyncMock
    from server.automations.backlog_runner import BacklogRunner, BacklogRunnerLoop

    ctx = SimpleNamespace(list_target_states=AsyncMock(return_value={}))
    loop = object.__new__(BacklogRunnerLoop)
    loop._ctx = ctx
    loop._last_pick = None
    loop._waiting_reason = None
    loop._count_candidates = AsyncMock(return_value=(0, 0, 0, {}))
    await loop._stash_counts({})
    first = ctx.observed_at
    ctx.list_target_states.side_effect = RuntimeError("claim read failed")
    loop._count_candidates.side_effect = RuntimeError("queue read failed")
    await loop._stash_counts({})
    failed = observed_status(ctx, BacklogRunner().status(ctx)).to_dict()
    assert failed["level"] == "unknown" and failed["counts"] is None
    assert failed["observedAt"] == first
    ctx.list_target_states.side_effect = None
    await loop._stash_counts({})
    partial = observed_status(ctx, BacklogRunner().status(ctx)).to_dict()
    assert partial["level"] == "warn"
    assert partial["counts"] == {"in_flight": 0, "needs_human": 0}
    assert partial["products"] is None
    assert "lastRefreshFailedAt" not in partial


@pytest.mark.asyncio
async def test_pr_doctor_failed_actionable_read_invalidates_previous_count():
    from unittest.mock import AsyncMock
    from server.sessions.orchestrator import SessionOrchestrator

    orch = object.__new__(SessionOrchestrator)
    orch._pr_doctor_ctx = SimpleNamespace(list_target_states=AsyncMock(return_value={}))
    orch._schedule_allows_tick = AsyncMock(return_value=True)
    orch._pr_doctor_scope_pairs = AsyncMock(return_value=[])
    orch.automation_actionable = AsyncMock(return_value=[{"id": "p"}])
    await orch._pr_doctor_sweep()
    first = orch.automation_statuses()["pr-doctor"]
    assert first["counts"] == {"failing": 1}
    assert orch.automation_statuses()["pr-doctor"] == first
    orch.automation_actionable.side_effect = RuntimeError("private provider failure")
    await orch._pr_doctor_sweep()
    failed = orch.automation_statuses()["pr-doctor"]
    assert failed["level"] == "unknown" and failed["counts"] is None
    assert failed["observedAt"] == first["observedAt"]
    assert "private" not in str(failed)

    orch._pr_doctor_scope_pairs.side_effect = RuntimeError("scope read failed")
    with pytest.raises(RuntimeError, match="scope read failed"):
        await orch._pr_doctor_sweep()
    assert orch.automation_statuses()["pr-doctor"]["observedAt"] == first["observedAt"]
