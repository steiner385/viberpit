"""Cancellation reconciles the real queue/hold before native entry on both DBs."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from server.automations.ownership import CanonicalTarget, ReleaseProof, StaleOwnership, acquire, owned
from server.db import models
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_automation_ownership import ownership_db as ownership_db


async def queued_or_held(ownership_db, tmp_path, monkeypatch, path):
    import server.db as database
    async with database._engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))
    orch._active_session_count = lambda: 1 if path == "queue" else 0
    orch._max_active_sessions = AsyncMock(return_value=1)
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", AsyncMock(
        return_value="allow" if path == "queue" else "require_approval"))
    async with acquire([CanonicalTarget("I_pending", "o/r#1")], owner="runner", source="test",
                       reason="dispatch", provenance={"origin_ref": "o/r#1"}) as tx:
        token = tx.token
    rec = await orch.create_session(name="pending", cwd=str(tmp_path), account="test", model="sonnet",
                                    origin="runner", origin_ref="o/r#1", ownership_token=token)
    assert rec.status == ("queued" if path == "queue" else "pending_approval")
    assert rec.id not in orch._drivers
    return orch, token, rec


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["queue", "hold"])
async def test_cancelled_real_root_releases_and_fences_later_native_entry(ownership_db, tmp_path, monkeypatch, path):
    orch, token, rec = await queued_or_held(ownership_db, tmp_path, monkeypatch, path)
    try:
        async with owned(token) as tx:
            assert not await _try_pending_proof(tx, rec.id)
        if path == "queue":
            assert await orch.session_queue_cancel(rec.id)
        else:
            await orch.resolve_model_approval(rec.id, "deny", identity="test")
        assert await orch._automation_session_work_stopped(rec.id)
        async with owned(token) as tx:
            tx.release(ReleaseProof.WORK_STOPPED, "cancelled before native entry")
        with pytest.raises(StaleOwnership):
            await orch._automation_before_execution(rec.id, execution_id="late-launch")
    finally:
        await orch.stop()


async def _try_pending_proof(tx, sid):
    # Inspect the actual durable reserve state, not an invented absent-driver proof.
    assert tx.operation.execution_sessions[sid]["current"] == "pending"
    assert tx.operation.execution_generation == 0
    with pytest.raises(ValueError):
        tx.release(ReleaseProof.WORK_STOPPED, "still waiting")
    return False


@pytest.mark.asyncio
async def test_real_queue_drain_losing_cancel_race_cannot_enter_native(ownership_db, tmp_path, monkeypatch):
    orch, token, rec = await queued_or_held(ownership_db, tmp_path, monkeypatch, "queue")
    entered, proceed = asyncio.Event(), asyncio.Event()
    effects = []
    async def spawn(sid, **kwargs):
        entered.set()
        await proceed.wait()
        await orch._automation_before_execution(sid, execution_id="drain")
        effects.append(sid)
        return rec
    orch._active_session_count = lambda: 0
    orch._spawn_session = AsyncMock(side_effect=spawn)
    drain = asyncio.create_task(orch._maybe_start_queued())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await orch.session_queue_cancel(rec.id)
        assert await orch._automation_session_work_stopped(rec.id)
        async with owned(token) as tx:
            tx.release(ReleaseProof.WORK_STOPPED, "cancel beat delayed native entry")
        proceed.set()
        await drain
        assert effects == []
    finally:
        proceed.set()
        await drain
        await orch.stop()


@pytest.mark.asyncio
async def test_stale_real_hold_approval_after_denial_cannot_enter_native(ownership_db, tmp_path, monkeypatch):
    import copy
    orch, token, rec = await queued_or_held(ownership_db, tmp_path, monkeypatch, "hold")
    stale = SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))
    stale._records[rec.id] = copy.deepcopy(rec)
    stale._pending_approvals[rec.id] = dict(orch._pending_approvals[rec.id])
    stale._max_active_sessions = AsyncMock(return_value=1)
    effects = []
    async def spawn(sid, **kwargs):
        await stale._automation_before_execution(sid, execution_id="late-approval")
        effects.append(sid)
        return rec
    stale._spawn_session = AsyncMock(side_effect=spawn)
    try:
        await orch.resolve_model_approval(rec.id, "deny", identity="test")
        assert await orch._automation_session_work_stopped(rec.id)
        async with owned(token) as tx:
            tx.release(ReleaseProof.WORK_STOPPED, "denial beat stale approver")
        with pytest.raises(StaleOwnership):
            await stale.resolve_model_approval(rec.id, "approve", identity="test")
        assert effects == []
    finally:
        await stale.stop()
        await orch.stop()
