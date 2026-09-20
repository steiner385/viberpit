"""Durable observers account accepted work before releasing a late callback's token."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from server.automations import mechanical_update as update
from server.automations.ownership import owned
from server.db.models import AutomationOperation
from server.tests.test_automation_ownership import ownership_db as ownership_db
from server.tests.test_pr_ownership import doctor as doctor, info
from server.tests.test_mechanical_update_ownership import HEAD, NEXT, payload, proc


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [False, True])
async def test_native_completed_observer_before_launch_ack_counts_once(doctor, ownership_db, lost):
    entered, respond = asyncio.Event(), asyncio.Event()

    async def spawn(*args, ownership_token, **kwargs):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
            tx.bind_session(sid)
            tx.operation.execution_generation = 1
            tx.operation.stopped_generation = 1
        entered.set()
        await respond.wait()
        if lost:
            raise RuntimeError('lost launch response')
        return 'spawned', sid, None

    doctor._pr_doctor_session_fix_owned = spawn
    doctor._automation_session_work_stopped.return_value = True
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    task = asyncio.create_task(doctor._pr_doctor_session_fix('o/r', 'work', info(), 'failing_ci', None))
    await entered.wait()
    await doctor._pr_doctor_sweep()
    state = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    assert state['total_attempts'] == 1
    assert 'ownership_token' not in state
    respond.set()
    if lost:
        with pytest.raises(RuntimeError, match='lost launch'):
            await task
    else:
        assert (await task)[0] == 'spawned'
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == state
    async with ownership_db() as db:
        operation = (await db.scalars(select(AutomationOperation))).one()
        assert operation.provenance['pr_attempt']['accounted'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_submit', [False, True])
async def test_mechanical_observer_before_ack_counts_once(doctor, monkeypatch, pause_submit):
    entered, respond = asyncio.Event(), asyncio.Event()
    submitted = False
    calls = []

    async def request(repo, args):
        nonlocal submitted
        calls.append(args)
        if '-X' in args:
            submitted = True
            if not pause_submit:
                entered.set()
                await respond.wait()
            return proc({'message': 'Updating pull request branch'})
        if '/pulls/' in args[0]:
            return proc(payload(NEXT if submitted else HEAD))
        ancestor = args[0].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead', 'base_commit': {'sha': ancestor},
                     'merge_base_commit': {'sha': ancestor}})

    monkeypatch.setattr(update, 'request', request)
    if pause_submit:
        submit = update.submit
        async def late(*args, **kwargs):
            result = await submit(*args, **kwargs)
            entered.set()
            await respond.wait()
            return result
        monkeypatch.setattr(update, 'submit', late)
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    task = asyncio.create_task(doctor._pr_doctor_mechanical('o/r', info(), 'out_of_date'))
    await entered.wait()
    await doctor._pr_doctor_sweep()
    state = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    assert state['total_mechanical'] == 1
    assert 'ownership_token' not in state
    respond.set()
    assert await task
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == state
    assert sum('-X' in args for args in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic', [False, True])
async def test_new_claim_accounts_delayed_predecessor_before_transfer(doctor, automatic):
    from server.automations.ownership import StaleOwnership
    entered, respond = asyncio.Event(), asyncio.Event()

    async def spawn(*args, ownership_token, **kwargs):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
            tx.bind_session(sid)
            tx.operation.execution_generation = tx.operation.stopped_generation = 1
        entered.set()
        await respond.wait()
        return 'spawned', sid, None

    doctor._pr_doctor_session_fix_owned = spawn
    doctor._automation_session_work_stopped.return_value = True
    task = asyncio.create_task(doctor._pr_doctor_session_fix('o/r', 'work', info(), 'failing_ci', None))
    await entered.wait()
    before = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    if automatic:
        with pytest.raises(StaleOwnership, match='refresh recovery decision'):
            await doctor._pr_doctor_claim('o/r', [1], 'o/r#1', expected_state=before)
        after = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
        assert after['ownership_token'] == before['ownership_token']
    else:
        after = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1', source='manual')
        assert after['ownership_token'] != before['ownership_token']
    assert after['total_attempts'] == 1
    respond.set()
    assert (await task)[0] == 'spawned'
    assert await doctor._pr_doctor_ctx.get_target_state('o/r#1') == after


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, 'wrong_uuid', 'wrong_origin', 'stale_proof'])
async def test_reserved_persisted_session_recovers_accounting_only_with_current_proof(doctor, invalid):
    from server.db.models import Session
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    await doctor._pr_doctor_prepare_attempt('o/r#1', state, {**state, 'total_attempts': 1}, 'native')
    async with owned(state['ownership_token']) as tx:
        sid = tx.reserve_session()
        tx.operation.execution_generation = 2
        tx.operation.stopped_generation = 1 if invalid == 'stale_proof' else 2
        tx.db.add(Session(id='wrong' if invalid == 'wrong_uuid' else sid,
                          name='repair', account_name='test', cwd='/tmp',
                          origin='other' if invalid == 'wrong_origin' else 'pr-doctor',
                          origin_ref='o/r#1', status='complete'))
    doctor._automation_session_work_stopped.return_value = invalid != 'stale_proof'
    doctor._schedule_allows_tick = AsyncMock(return_value=False)
    await doctor._pr_doctor_sweep()
    after = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    if invalid is None:
        assert after['total_attempts'] == 1
        assert 'ownership_token' not in after
    else:
        assert after == state
        async with owned(state['ownership_token']) as tx:
            assert tx.operation.session_id is None
            assert not tx.operation.provenance['pr_attempt']['accounted']
        from server.automations.ownership import OwnershipConflict
        with pytest.raises(OwnershipConflict):
            await doctor._pr_doctor_claim('o/r', [1], 'o/r#1', source='manual')


@pytest.mark.asyncio
@pytest.mark.parametrize('transfer', [False, True])
async def test_runner_terminal_action_settles_pr_predecessor_before_relinquishing(doctor, ownership_db, transfer):
    from server.automations.context import AutomationContext
    from server.automations.ownership import ReleaseProof
    from server.db.models import Automation, AutomationTargetState
    state = await doctor._pr_doctor_claim('o/r', [1], 'o/r#1')
    await doctor._pr_doctor_prepare_attempt('o/r#1', state, {**state, 'total_attempts': 1}, 'native')
    runner_state = {'ownership_token': state['ownership_token'], 'handed_off': True}
    async with owned(state['ownership_token']) as tx:
        sid = tx.reserve_session()
        tx.bind_session(sid)
        tx.operation.execution_generation = tx.operation.stopped_generation = 1
        tx.db.add(Automation(id='backlog-runner'))
        await tx.db.flush()
        tx.db.add(AutomationTargetState(automation_id='backlog-runner', target='o/r#7', state=runner_state))
    ctx = AutomationContext('backlog-runner')
    if transfer:
        assert await ctx.reserve_target_action('o/r#7', runner_state, source='terminal-closeout',
                                                reason='PR merged', state=runner_state)
    else:
        assert await ctx.compare_target_state('o/r#7', runner_state, None,
                                              release_proof=ReleaseProof.WORK_STOPPED, reason='complete')
    after = await doctor._pr_doctor_ctx.get_target_state('o/r#1')
    assert after['total_attempts'] == 1
    assert await doctor._pr_doctor_finish('o/r#1', state, state)
