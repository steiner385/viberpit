import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from server import quick_replies as qr
from server.db import db_session
from server.db.models import QuickReplyProfile

pytestmark = pytest.mark.usefixtures('isolated_db')


@pytest.mark.asyncio
async def test_personal_grouping_counts_and_no_feedback(monkeypatch):
    curate = AsyncMock(return_value=json.dumps([
        {'label': 'Recommend an approach', 'members': [2]},
        {'label': 'Continue', 'members': [0, 1]},
    ]))
    monkeypatch.setattr(qr, '_curate', curate)
    for text in ['continue', 'please continue', 'what do you recommend']:
        await qr.observe_reply('alice', text)
    await qr.observe_reply('alice', 'continue', source='suggested')
    await qr.observe_reply('bob', 'try again')
    assert await qr.get_quick_replies('alice') == ['Continue', 'Recommend an approach']
    assert curate.call_args.args[0] == ['continue', 'please continue', 'what do you recommend']
    assert await qr.get_quick_replies('nobody') == []
    assert await qr.get_quick_replies('alice') == ['Continue', 'Recommend an approach']
    assert curate.await_count == 1


@pytest.mark.parametrize('raw', [
    '[{"label":"Continue","members":[0,0]}]',
    '[{"label":"Continue","members":[99]}]',
    '[{"label":"Continue","members":[true]}]',
    '[{"label":"Continue","members":[0]},{"label":"Again","members":[0]}]',
    '[{"label":"Continue","members":[0]},{"label":"continue","members":[1]}]',
    'not json',
])
def test_reject_malformed_membership(raw):
    assert qr._rank_clusters(raw, 2) is None


@pytest.mark.asyncio
async def test_filter_provenance_and_sensitive_inputs():
    for kwargs in [{'source': 'suggested'}, {'source': 'unknown'}, {'via': 'assistant'},
                   {'via': 'internal'}, {'via': 'cli'}, {'has_attachments': True}]:
        await qr.observe_reply('alice', 'continue', **kwargs)
    for text in ['fix server/app.py', 'my password is huntertwo', 'https://example.org',
                 'change this\nand that', 'a' * 500]:
        await qr.observe_reply('alice', text)
    await qr.observe_reply('system', 'continue')
    await qr.observe_reply('lan@local', 'continue')
    async with db_session() as db:
        assert list(await db.scalars(select(QuickReplyProfile))) == []


@pytest.mark.asyncio
async def test_concurrent_writes_bounded_and_failure_keeps_cache(monkeypatch):
    monkeypatch.setattr(qr, '_MAX_SAMPLES', 10)
    await asyncio.gather(*(qr.observe_reply('alice', 'continue') for _ in range(20)))
    async with db_session() as db:
        row = await db.get(QuickReplyProfile, 'alice')
        assert len(row.samples) == 10
        row.replies = ['Continue']
    monkeypatch.setattr(qr, '_curate', AsyncMock(side_effect=RuntimeError('secret payload')))
    assert await qr.get_quick_replies('alice') == ['Continue']
    assert await qr.get_quick_replies('alice') == ['Continue']
    assert qr._curate.await_count == 1


@pytest.mark.asyncio
async def test_parallel_reads_only_one_helper_and_expiry(monkeypatch):
    now = 1000000000.0
    monkeypatch.setattr(qr.time, 'time', lambda: now)
    await qr.observe_reply('alice', 'continue')
    started = asyncio.Event()
    finish = asyncio.Event()

    async def curate(samples):
        started.set()
        await finish.wait()
        return '[{"label":"Continue","members":[0]}]'

    monkeypatch.setattr(qr, '_curate', curate)
    first = asyncio.create_task(qr.get_quick_replies('alice'))
    await started.wait()
    assert await qr.get_quick_replies('alice') == []
    finish.set()
    assert await first == ['Continue']
    now += qr._RETENTION_S + 1
    assert await qr.get_quick_replies('alice') == []


@pytest.mark.asyncio
async def test_task_specific_group_omitted_and_top_five(monkeypatch):
    for text in ['please continue', 'please reconsider', 'please explain',
                 'please review', 'please simplify', 'please stop', 'fix frobnicator']:
        await qr.observe_reply('alice', text)
    groups = [{'label': label, 'members': [i]} for i, label in enumerate(
        ['Continue', 'Reconsider', 'Explain', 'Review', 'Simplify', 'Stop'])]
    monkeypatch.setattr(qr, '_curate', AsyncMock(return_value=json.dumps(groups)))
    assert await qr.get_quick_replies('alice') == ['Continue', 'Reconsider', 'Explain', 'Review', 'Simplify']


def test_api_uses_authenticated_identity(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api import quick_replies as api
    from server.auth.middleware import current_identity

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_identity] = lambda: 'alice'
    lookup = AsyncMock(return_value=['Continue'])
    monkeypatch.setattr(api, 'get_quick_replies', lookup)
    with TestClient(app) as client:
        assert client.get('/api/quick-replies?identity=bob').json() == {'replies': ['Continue']}
    lookup.assert_awaited_once_with('alice')


def test_api_requires_identity():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.quick_replies import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/quick-replies').status_code == 401


@pytest.mark.asyncio
async def test_no_helper_account_fails_closed(monkeypatch):
    from server.sessions import helper_call
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: None)
    assert await qr._curate(['continue']) is None


@pytest.mark.asyncio
async def test_expired_members_invalidate_cache_during_cooldown(monkeypatch):
    now = 1000000000.0
    monkeypatch.setattr(qr.time, 'time', lambda: now)
    await qr.observe_reply('alice', 'continue')
    async with db_session() as db:
        row = await db.get(QuickReplyProfile, 'alice')
        row.samples = [{'text': 'try again', 'at': now - qr._RETENTION_S - 1},
                       {'text': 'continue', 'at': now - 10}]
        row.replies = ['Try again']
        row.curated_at = now - 5
        row.attempted_at = now - 5
    curate = AsyncMock(return_value='[{"label":"Continue","members":[0]}]')
    monkeypatch.setattr(qr, '_curate', curate)
    assert await qr.get_quick_replies('alice') == ['Try again']
    now += qr._REFRESH_S
    assert await qr.get_quick_replies('alice') == ['Continue']
    curate.assert_awaited_once_with(['continue'])


@pytest.mark.asyncio
async def test_curator_cannot_use_inherited_tools_or_persist_samples(monkeypatch):
    import types
    import claude_agent_sdk
    from server.sessions import account_auth, claude_bin, helper_call

    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: '/test/account')
    monkeypatch.setattr(helper_call, 'helper_cwd', lambda: '/test/scratch')
    monkeypatch.setattr(account_auth, 'helper_env', lambda _: {})
    monkeypatch.setattr(claude_bin, 'cli_path_kwargs', lambda: {})
    seen = []

    async def query(*, prompt, options):
        seen.append(options)
        assert json.loads(prompt) == ['please continue']
        yield types.SimpleNamespace(content=[types.SimpleNamespace(text='[]')])

    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    assert await qr._curate(['please continue']) == '[]'
    options = seen[0]
    assert options.thinking == {'type': 'disabled'}
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert options.setting_sources == []
    assert options.extra_args == {'no-session-persistence': None}


def test_rank_native_helper_fenced_output_preserves_semantics():
    raw = '''```json
    [
      {"label":"Continue", "members":[0,1,2]},
      {"label":"Approved", "members":[3,4]},
      {"label":"Do not continue", "members":[5]},
      {"label":"What do you recommend?", "members":[6,7]}
    ]
    ```'''
    assert qr._rank_clusters(raw, 9) == [
        'Continue', 'Approved', 'What do you recommend?', 'Do not continue',
    ]
    assert qr._rank_clusters('```json\n[{"label":"Continue","members":[0,0]}]\n```', 2) is None
