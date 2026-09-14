"""Runner terminal ownership transitions detach only the matching PR state."""
from unittest.mock import AsyncMock

import pytest

from server.automations.context import AutomationContext
from server.automations.ownership import ReleaseProof, owned
from server.db.models import Automation, AutomationTargetState
from server.tests.test_automation_ownership import ownership_db as ownership_db
from server.tests.test_pr_ownership import doctor as doctor


async def handoff(doctor):
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    await doctor._pr_doctor_prepare_attempt('o/r#1', state,
        {**state, 'total_attempts': 2, 'lifetime_attempts': 3}, 'native')
    runner_state = {'ownership_token': state['ownership_token'], 'handed_off': True}
    async with owned(state['ownership_token']) as tx:
        sid = tx.reserve_session()
        tx.bind_session(sid)
        tx.operation.execution_generation = tx.operation.stopped_generation = 1
        tx.operation.provenance = {**tx.operation.provenance, 'runner_target': 'o/r#7'}
        tx.db.add(Automation(id='backlog-runner'))
        await tx.db.flush()
        tx.db.add(AutomationTargetState(automation_id='backlog-runner', target='o/r#7', state=runner_state))
    return state, runner_state, AutomationContext('backlog-runner')


@pytest.mark.asyncio
@pytest.mark.parametrize('transfer', [False, True])
async def test_runner_closeout_detaches_pr_then_sweep_and_new_claim_work(doctor, transfer):
    original, runner_state, ctx = await handoff(doctor)
    if transfer:
        runner_state = await ctx.reserve_target_action('o/r#7', runner_state, source='terminal-closeout',
                                                       reason='PR merged', state=runner_state)
        assert runner_state is not None
    assert await ctx.compare_target_state('o/r#7', runner_state, None,
        release_proof=ReleaseProof.WORK_STOPPED, reason='closeout completed', action_completed=transfer)
    history = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    assert history == {'total_attempts': 2, 'lifetime_attempts': 3}
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    await doctor._pr_doctor_sweep()
    claimed = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1', source='manual')
    assert claimed['ownership_token'] != original['ownership_token']
    assert claimed['total_attempts'] == 2 and claimed['lifetime_attempts'] == 3
    # Delayed successful callback acknowledges retained predecessor bookkeeping.
    assert await doctor._pr_doctor_finish('o/r#1', original, original)
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == claimed


@pytest.mark.asyncio
@pytest.mark.parametrize('transfer', [False, True])
@pytest.mark.parametrize('accounted', [False, True])
async def test_old_transition_never_detaches_newer_pr_row(doctor, transfer, accounted):
    from server.automations.pr_accounting import account
    original, runner_state, ctx = await handoff(doctor)
    newer = {'ownership_token': 'new-owner', 'session_id': 'new-session', 'total_attempts': 9}
    async with owned(original['ownership_token']) as tx:
        if accounted:
            assert await account(tx, 'o/r#1')
        row = await tx.db.get(AutomationTargetState, ('pr-doctor', 'o/r#1'))
        row.state = newer
    if transfer:
        result = await ctx.reserve_target_action('o/r#7', runner_state, source='terminal-closeout',
                                                 reason='PR merged', state=runner_state)
    else:
        result = await ctx.compare_target_state('o/r#7', runner_state, None,
            release_proof=ReleaseProof.WORK_STOPPED, reason='complete')
    assert bool(result) is accounted
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == newer


@pytest.mark.asyncio
async def test_stale_runner_snapshot_cannot_detach_matching_pr_row(doctor):
    original, runner_state, ctx = await handoff(doctor)
    assert await ctx.reserve_target_action('o/r#7', {**runner_state, 'stale': True},
        source='terminal-closeout', reason='PR merged', state=runner_state) is None
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == original
