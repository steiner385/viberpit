"""Retained manual updates settle without an open pointer or another user click."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from server.automations import mechanical_update as update
from server.automations.ownership import CanonicalTarget, OwnershipConflict
from server.db.models import AutomationOperation
from server.pm.map_changes import MapChangesService
from server.pm.map_changes_gh import GhWrite
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_automation_ownership import ownership_db as ownership_db
from server.tests.test_mechanical_update_ownership import NEXT, claim, payload, proc
from server.tests.test_pm_map_changes_gh import PAT_ENV


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal,proof', [(False, True), (True, True), (True, False)])
async def test_periodic_tick_settles_retained_manual_update_without_pointer(
    ownership_db, monkeypatch, terminal, proof,
):
    monkeypatch.setattr('server.automations.target_identity.resolve_targets',
                        AsyncMock(return_value=(CanonicalTarget('PR_1', 'o/r#1'),)))
    monkeypatch.setattr('server.automations.ownership_reconciliation.reconcile_legacy_targets', AsyncMock())
    request = AsyncMock(side_effect=[proc(payload()), proc({'message': 'Updating'})])
    monkeypatch.setattr(update, 'request', request)
    assert await update.manual_update('o/r', 1, project_id='product-1') == 'accepted'
    with pytest.raises(OwnershipConflict):
        await claim()
    reads = []

    def run(args, token, **kwargs):
        reads.append((args, token))
        assert args[:3] == ['api', '--hostname', 'github.com']
        assert '-X' not in args  # Background tick must never replay the mutation.
        if token == 'app':
            return proc({}, 1, 'Resource not accessible by integration (HTTP 403)')
        assert token == 'pat-tok'  # Preserve the manual product credential fallback.
        if '/pulls/' in args[3]:
            return proc({**payload(NEXT), 'state': 'closed' if terminal else 'open'})
        ancestor = args[3].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead' if proof else 'diverged',
                     'base_commit': {'sha': ancestor}, 'merge_base_commit': {'sha': ancestor}})

    service = MapChangesService(gh_factory=lambda repo: GhWrite(
        repo, run=run, app_token=lambda owner: 'app', env=PAT_ENV))
    orch = object.__new__(SessionOrchestrator)
    orch.pm_map_changes = lambda: service
    orch._schedule_allows_tick = AsyncMock(return_value=False)
    orch._pr_doctor_enabled = AsyncMock(return_value=False)
    # No project pointer exists at all; a terminal pointer may have disappeared
    # from ordinary PM reads. Reconciliation comes from durable operation rows.
    await orch._pm_sync_tick_once()
    orch._pr_doctor_enabled.assert_not_awaited()
    assert len(reads) >= 2
    assert request.await_count == 2  # Exactly one original snapshot and one PUT.
    async with ownership_db() as db:
        op = (await db.scalars(select(AutomationOperation))).one()
        assert op.provenance['project_id'] == 'product-1'
        assert op.phase == ('released' if proof else 'uncertain')
    if proof:
        await claim()
    else:
        with pytest.raises(OwnershipConflict):
            await claim()


@pytest.mark.asyncio
async def test_background_completion_racing_submission_tail_is_idempotent(ownership_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr('server.automations.target_identity.resolve_targets',
                        AsyncMock(return_value=(CanonicalTarget('PR_1', 'o/r#1'),)))
    monkeypatch.setattr('server.automations.ownership_reconciliation.reconcile_legacy_targets', AsyncMock())
    submitted, finish = asyncio.Event(), asyncio.Event()

    async def request(repo, args):
        if '-X' in args:
            submitted.set()
            await finish.wait()
            return proc({'message': 'Updating'})
        return proc(payload())

    monkeypatch.setattr(update, 'request', request)
    task = asyncio.create_task(update.manual_update('o/r', 1, project_id='product-1'))
    await submitted.wait()

    def read(args):
        assert '-X' not in args
        if '/pulls/' in args[0]:
            return proc(payload(NEXT))
        ancestor = args[0].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead', 'base_commit': {'sha': ancestor},
                     'merge_base_commit': {'sha': ancestor}})

    service = MapChangesService(gh_factory=lambda repo: SimpleNamespace(recovery_request=read))
    await service.reconcile_updates()
    finish.set()
    assert await task == 'completed'
    await claim()
