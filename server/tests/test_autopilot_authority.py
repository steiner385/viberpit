"""Mutations must recheck real session rows and fenced leases after model awaits."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from server.automations.autopilot_policy import Decision
from server.db import init_db, db_session
from server.db.models import Session
from server.sessions import primary_lease, session_lease
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.autopilot_authority import decision_context, seed_authority


@pytest_asyncio.fixture
async def actor(tmp_path, monkeypatch):
    monkeypatch.setenv('VIBERPIT_DAEMON_ROLE', 'worker')
    monkeypatch.setenv('VIBERPIT_HOST_NAME', 'rocky')
    await init_db(db_url=f'sqlite+aiosqlite:///{tmp_path}/authority.db')
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {'s1': SessionRecord(id='s1', name='test', account='a', engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id='', cols=80, rows=24,
                                        cwd='/tmp', host='rocky', status='idle')}
    orch._drivers = {}
    orch._turn_restart_at = {}
    orch._autopilot_mark_assisted = AsyncMock()
    orch.send_prompt = AsyncMock(return_value='sent')
    orch.archive_session = AsyncMock()
    orch._autopilot_pr_facts = lambda rec: None
    orch._enabled_host_names = AsyncMock(return_value=frozenset({'rocky', 'dobby'}))

    async def ensure(sid, identity):
        driver = SimpleNamespace(identity=identity)
        orch._drivers[sid] = driver
        orch._lease_fences()[sid] = await session_lease.acquire(sid, 'rocky', ttl_s=120)
        return driver

    orch.ensure_driver = ensure
    await seed_authority(orch)
    return orch


@pytest.mark.asyncio
async def test_owned_idle_session_can_act_and_archive(actor):
    ctx = decision_context(actor._records['s1'])
    assert await actor._autopilot_apply('s1', Decision(action='nudge', prompt='continue'), ctx)
    actor.send_prompt.assert_awaited_once()
    assert await actor._autopilot_archive('s1', ctx)
    actor.archive_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['human', 'status', 'handback', 'pause', 'drain', 'host', 'lease', 'hb', 'global'])
async def test_changes_during_driver_await_prevent_apply(actor, change):
    ctx = decision_context(actor._records['s1'])
    ensure = actor.ensure_driver

    async def change_during_ensure(sid, identity):
        driver = await ensure(sid, identity)
        rec = actor._records[sid]
        if change == 'human':
            rec.last_user_interaction_at = datetime.utcnow()
        elif change == 'status':
            rec.status = 'in_progress'
        elif change == 'handback':
            rec.status = 'handed_back'
        elif change == 'pause':
            rec.autopilot_paused = True
        elif change == 'drain':
            actor._draining = True
        elif change == 'hb':
            actor._hb_fenced = True
        elif change == 'global':
            from server.db.models import Automation
            async with db_session() as db:
                row = await db.get(Automation, 'autopilot')
                row.mode = 'off'
        elif change == 'host':
            async with db_session() as db:
                row = await db.get(Session, sid)
                row.host = 'dobby'
        elif change == 'lease':
            await session_lease.release(sid, 'rocky', actor._lease_fences()[sid])
            await session_lease.acquire(sid, 'dobby', ttl_s=120)
        return driver

    actor.ensure_driver = change_during_ensure
    assert not await actor._autopilot_apply('s1', Decision(action='nudge', prompt='continue'), ctx)
    actor.send_prompt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['human', 'status', 'handback', 'pause', 'drain', 'host', 'hb', 'optout'])
async def test_stale_completion_never_archives(actor, change):
    ctx = decision_context(actor._records['s1'])
    rec = actor._records['s1']
    if change == 'human':
        rec.last_user_interaction_at = datetime.utcnow()
    elif change in ('status', 'handback'):
        rec.status = 'handed_back' if change == 'handback' else 'in_progress'
    elif change == 'pause':
        rec.autopilot_paused = True
    elif change == 'drain':
        actor._draining = True
    elif change == 'hb':
        actor._hb_fenced = True
    elif change == 'optout':
        rec.auto_archive_opt_out_reason = 'manual-unarchive'
    else:
        async with db_session() as db:
            row = await db.get(Session, 's1')
            row.host = 'dobby'
    assert not await actor._autopilot_archive('s1', ctx)
    actor.archive_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_failure_fails_closed(actor, monkeypatch):
    monkeypatch.setattr(session_lease, 'current', AsyncMock(side_effect=OSError('database down')))
    assert not await actor._autopilot_may_act('s1', decision_context(actor._records['s1']))


@pytest.mark.asyncio
async def test_primary_lease_fence_loss_fails_closed(actor, monkeypatch):
    monkeypatch.setenv('VIBERPIT_DAEMON_ROLE', 'primary')
    await seed_authority(actor)
    ctx = decision_context(actor._records['s1'])
    assert await actor._autopilot_may_act('s1', ctx)
    await primary_lease.release('rocky', actor._lease_fence)
    await primary_lease.acquire('dobby', ttl_s=300)
    assert not await actor._autopilot_may_act('s1', ctx)


@pytest.mark.asyncio
async def test_completion_recheck_excludes_foreign_mirror(actor):
    actor._records['s1'].status = 'complete'
    actor._records['foreign'] = SessionRecord(
        id='foreign', name='mirror', account='a', engine=AgentEngine.CLAUDE_CODE, engine_session_id='', cols=80,
        rows=24, cwd='/tmp', host='dobby', status='complete')
    actor._auto_archiving = set()
    actor._complete_nudge_exhausted = lambda sid: True
    actor.recheck_stuck_complete_session = AsyncMock()
    await actor._recheck_stuck_complete_sessions_once()
    actor.recheck_stuck_complete_session.assert_awaited_once_with('s1')


@pytest.mark.asyncio
async def test_worker_periodic_loop_retries_deference_without_status_event(actor):
    import asyncio
    import time
    from server.aio import cancel_and_reap
    from server.automations.autopilot import AutopilotLoop
    from server.automations.context import AutomationContext

    ctx = AutomationContext('autopilot')
    reads = 0
    sent = asyncio.Event()

    async def context(sid):
        nonlocal reads
        reads += 1
        dc = decision_context(actor._records[sid])
        dc.seconds_since_human = 10 if reads == 1 else 130
        return dc

    async def send(sid, prompt, **kw):
        actor._records[sid].status = 'in_progress'
        sent.set()
        return 'sent'

    actor.send_prompt = AsyncMock(side_effect=send)
    actor._autopilot_ctx = ctx
    actor._recover_resume_failures_once = AsyncMock()
    actor._recheck_stuck_complete_sessions_once = AsyncMock()
    actor._autopilot_loop = AutopilotLoop(
        ctx, read_config=actor._autopilot_read_config,
        list_governed=actor._autopilot_list_governed, get_context=context,
        apply=actor._autopilot_apply, may_act=actor._autopilot_may_act,
        is_current=actor._autopilot_context_current,
        decide_fn=AsyncMock(return_value=Decision(action='nudge', prompt='continue')),
        clock=time.time)
    task = asyncio.create_task(actor._autopilot_tick_loop(interval=0))
    try:
        async with asyncio.timeout(5):
            await sent.wait()
    finally:
        await cancel_and_reap(task, phase="autopilot-test")
    assert reads >= 2
    actor.send_prompt.assert_awaited_once()
    state = await ctx.get_target_state('s1')
    assert state['repeat_count'] <= 1


@pytest.mark.asyncio
async def test_concurrent_status_and_periodic_ticks_act_once(actor):
    import asyncio
    import time
    from server.automations.autopilot import AutopilotLoop
    from server.automations.context import AutomationContext

    async def context(sid):
        return decision_context(actor._records[sid])

    async def decide(dc):
        await asyncio.sleep(0)
        return Decision(action='nudge', prompt='continue')

    async def send(sid, prompt, **kw):
        actor._records[sid].status = 'in_progress'
        return 'sent'

    actor.send_prompt = AsyncMock(side_effect=send)
    ctx = AutomationContext('autopilot')
    actor._autopilot_ctx = ctx
    loop = AutopilotLoop(
        ctx, read_config=actor._autopilot_read_config,
        list_governed=actor._autopilot_list_governed, get_context=context,
        apply=actor._autopilot_apply, may_act=actor._autopilot_may_act,
        is_current=actor._autopilot_context_current,
        decide_fn=decide, clock=time.time)
    await asyncio.gather(loop.tick(), loop.tick())
    actor.send_prompt.assert_awaited_once()
    state = await ctx.get_target_state('s1')
    assert state['repeat_count'] == 1
    assert len(state['hour_actions']) == 1
