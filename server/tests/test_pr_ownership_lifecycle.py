"""Terminal actors and disappeared PRs still discharge their durable claims."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.automations import mechanical_update as update
from server.automations.ownership import owned
from server.sessions.engine import AgentEngine
from server.tests.test_automation_ownership import ownership_db as ownership_db
from server.tests.test_pr_ownership import doctor as doctor, info
from server.tests.test_mechanical_update_ownership import HEAD, BASE, NEXT, payload, proc


async def stopped(doctor, state):
    async with owned(state['ownership_token']) as tx:
        sid = tx.reserve_session()
        tx.bind_session(sid)
        tx.operation.execution_generation = 1
        tx.operation.stopped_generation = 1
    doctor._automation_session_work_stopped.return_value = True
    return sid


@pytest.mark.asyncio
async def test_stopped_consolidation_without_state_sid_is_reconciled_outside_schedule(doctor):
    state = await doctor._pr_doctor_claim('o/r', [1, 2], 'consolidate:o/r#1+2')
    await stopped(doctor, state)
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    await doctor._pr_doctor_sweep()
    assert not (await doctor._pr_doctor_ctx.get_target_state('consolidate:o/r#1+2')).get('ownership_token')
    assert (await doctor._pr_doctor_claim('o/r', [2, 3], 'consolidate:o/r#2+3'))['ownership_token'] != state['ownership_token']


@pytest.mark.asyncio
async def test_standalone_recovery_clear_releases_for_overlapping_consolidation(doctor):
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    await stopped(doctor, state)
    await doctor._pr_doctor_clear_observed('o/r#1', state)
    assert (await doctor._pr_doctor_claim('o/r', [1, 2], 'consolidate:o/r#1+2'))['ownership_token'] != state['ownership_token']


@pytest.mark.asyncio
async def test_successful_consolidation_persists_bound_session_id(doctor):
    state = await doctor._pr_doctor_claim('o/r', [1, 2], 'consolidate:o/r#1+2')
    doctor._pr_doctor_ctx.launch_plan_or_refuse = AsyncMock(return_value=(AgentEngine.CODEX, {}))
    doctor._sdlc_default_branch = AsyncMock(return_value='master')

    async def spawn(**kwargs):
        async with owned(kwargs['ownership_token']) as tx:
            sid = tx.reserve_session()
            tx.bind_session(sid)
        return SimpleNamespace(id=sid)

    doctor._pr_doctor_ctx.spawn_session = spawn
    await doctor._pr_doctor_consolidate_spawn('o/r', 'consolidate:o/r#1+2', [info(1), info(2)], state)
    after = await doctor._pr_doctor_ctx.get_target_state('consolidate:o/r#1+2')
    async with owned(state['ownership_token']) as tx:
        assert after['session_id'] == tx.operation.session_id


@pytest.mark.asyncio
async def test_pending_update_is_observed_after_pr_disappears_from_population(doctor, monkeypatch):
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    monkeypatch.setattr(update, 'request', AsyncMock(return_value=proc({'message': 'Updating pull request branch'})))
    await update.submit(state['ownership_token'], {'repo': 'o/r', 'pr_number': 1,
        'expected_head_sha': HEAD, 'requested_base_sha': BASE, 'mechanical_kind': 'update_branch'})
    calls = []

    async def request(repo, args):
        calls.append(args)
        assert '-X' not in args, 'reconciliation must never resubmit'
        if '/pulls/' in args[0]:
            data = payload(NEXT)
            data['state'] = 'closed'
            return proc(data)
        ancestor = args[0].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead', 'base_commit': {'sha': ancestor},
                     'merge_base_commit': {'sha': ancestor}})

    monkeypatch.setattr(update, 'request', request)
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    doctor._pr_doctor_scope_pairs = AsyncMock(return_value=[])
    await doctor._pr_doctor_sweep()
    assert len(calls) == 3
    assert not (await doctor._pr_doctor_ctx.get_target_state('o/r#1')).get('ownership_token')
    await doctor._pr_doctor_claim('o/r', [1, 2], 'consolidate:o/r#1+2')


@pytest.mark.asyncio
async def test_observation_release_keeps_failure_budget_and_runner_handoff_owned(doctor):
    from server.db.models import AutomationTargetState
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    state = {**state, 'total_attempts': 3, 'lifetime_attempts': 4}
    async with owned(state['ownership_token']) as tx:
        row = await tx.db.get(AutomationTargetState, ('pr-doctor', 'o/r#1'))
        row.state = state
    await stopped(doctor, state)
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    await doctor._pr_doctor_sweep()
    after = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    assert after == {'total_attempts': 3, 'lifetime_attempts': 4}

    state = await doctor._pr_doctor_claim('o/r', [2], 'o/r#2')
    async with owned(state['ownership_token']) as tx:
        tx.operation.provenance = {**tx.operation.provenance, 'runner_target': 'o/r#7'}
    await stopped(doctor, state)
    await doctor._pr_doctor_sweep()
    assert (await doctor._pr_doctor_ctx.get_target_state('o/r#2'))['ownership_token'] == state['ownership_token']
