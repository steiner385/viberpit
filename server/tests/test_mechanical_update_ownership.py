"""GitHub accepted work remains fenced until pinned commit ancestry is observed."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.automations import mechanical_update as update
from server.automations.ownership import CanonicalTarget, OwnershipConflict, acquire, owned
from server.tests.test_automation_ownership import ownership_db as ownership_db

HEAD, BASE, NEXT = 'a' * 40, 'b' * 40, 'c' * 40


def payload(head=HEAD):
    return {'number': 1, 'state': 'open', 'html_url': 'https://github.com/o/r/pull/1',
            'head': {'sha': head}, 'base': {'sha': BASE, 'repo': {'full_name': 'o/r'}}}


def proc(data, rc=0, stderr=''):
    return SimpleNamespace(returncode=rc, stdout=json.dumps(data), stderr=stderr)


async def claim():
    async with acquire([CanonicalTarget('PR_1', 'o/r#1')], owner='pr-doctor',
                       source='test', reason='test') as tx:
        return tx.token


@pytest.mark.asyncio
async def test_accepted_blocks_overlapping_consolidation_until_pinned_ancestry(ownership_db, monkeypatch):
    token = await claim()
    calls = []
    gate, finish = asyncio.Event(), asyncio.Event()

    async def request(repo, args):
        calls.append(args)
        if '-X' in args:
            gate.set()
            await finish.wait()
            return proc({'message': 'Updating pull request branch.'})
        return proc(payload())

    monkeypatch.setattr(update, 'request', request)
    evidence = await update.snapshot('o/r', 1)
    task = asyncio.create_task(update.submit(token, evidence))
    await gate.wait()
    with pytest.raises(OwnershipConflict):
        async with acquire([CanonicalTarget('PR_1', 'o/r#1'), CanonicalTarget('PR_2', 'o/r#2')],
                           owner='consolidation', source='manual', reason='test'):
            pytest.fail('overlap acquired')
    finish.set()
    assert await task == 'accepted'
    assert f'expected_head_sha={HEAD}' in calls[1]
    assert not await update.observe(token)

    async def completed(repo, args):
        if '/pulls/' in args[0]:
            return proc(payload(NEXT))
        ancestor = args[0].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead', 'base_commit': {'sha': ancestor},
                     'merge_base_commit': {'sha': ancestor}})

    monkeypatch.setattr(update, 'request', completed)
    assert await update.observe(token)
    async with owned(token) as tx:
        assert tx.operation.phase == 'completed'
        assert tx.operation.provenance['requested_base_sha'] == BASE


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [(422, 'rejected'), (403, 'rejected'), (502, 'unknown')])
async def test_refusal_vs_ambiguous_response(ownership_db, monkeypatch, status, expected):
    token = await claim()
    evidence = {'repo': 'o/r', 'pr_number': 1, 'expected_head_sha': HEAD,
                'requested_base_sha': BASE, 'mechanical_kind': 'update_branch'}
    monkeypatch.setattr(update, 'request', AsyncMock(return_value=proc({}, 1, f'gh: failed (HTTP {status})')))
    assert await update.submit(token, evidence) == expected
    async with owned(token) as tx:
        assert tx.operation.phase == ('completed' if expected == 'rejected' else 'uncertain')


@pytest.mark.asyncio
async def test_changed_head_without_requested_base_does_not_complete(ownership_db, monkeypatch):
    token = await claim()
    async with owned(token) as tx:
        tx.operation.provenance = {'repo': 'o/r', 'pr_number': 1, 'expected_head_sha': HEAD,
                                   'requested_base_sha': BASE, 'mechanical_kind': 'update_branch'}
        tx.start_action()
    monkeypatch.setattr(update, 'request', AsyncMock(side_effect=[
        proc(payload(NEXT)), proc({'status': 'ahead', 'base_commit': {'sha': HEAD},
                                  'merge_base_commit': {'sha': HEAD}}),
        proc({'status': 'diverged', 'base_commit': {'sha': BASE}, 'merge_base_commit': {'sha': HEAD}})]))
    assert not await update.observe(token)


@pytest.mark.asyncio
async def test_manual_map_update_conflicts_with_pr_repair(ownership_db, monkeypatch):
    await claim()
    monkeypatch.setattr('server.automations.target_identity.resolve_targets',
                        AsyncMock(return_value=(CanonicalTarget('PR_1', 'o/r#1'),)))
    monkeypatch.setattr('server.automations.ownership_reconciliation.reconcile_legacy_targets', AsyncMock())
    monkeypatch.setattr(update, 'request', AsyncMock(return_value=proc(payload())))
    with pytest.raises(OwnershipConflict):
        await update.manual_update('o/r', 1)
    assert update.request.await_count == 1  # snapshot only, never PUT


@pytest.mark.asyncio
async def test_manual_retry_observes_pending_without_second_put(ownership_db, monkeypatch):
    monkeypatch.setattr('server.automations.target_identity.resolve_targets',
                        AsyncMock(return_value=(CanonicalTarget('PR_1', 'o/r#1'),)))
    monkeypatch.setattr('server.automations.ownership_reconciliation.reconcile_legacy_targets', AsyncMock())
    requests = AsyncMock(side_effect=[proc(payload()), proc({'message': 'Updating'})])
    monkeypatch.setattr(update, 'request', requests)
    assert await update.manual_update('o/r', 1) == 'accepted'
    requests.side_effect = [proc(payload())]
    with pytest.raises(OwnershipConflict, match='pending'):
        await update.manual_update('o/r', 1)
    requests.side_effect = [proc(payload(NEXT)),
                           proc({'status': 'ahead', 'base_commit': {'sha': HEAD},
                                 'merge_base_commit': {'sha': HEAD}}),
                           proc({'status': 'ahead', 'base_commit': {'sha': BASE},
                                 'merge_base_commit': {'sha': BASE}})]
    assert await update.manual_update('o/r', 1) == 'completed'
    assert sum('-X' in call.args[1] for call in requests.await_args_list) == 1
    await claim()  # Ownership was released only after observed completion.


@pytest.mark.asyncio
async def test_lost_response_retains_claim(ownership_db, monkeypatch):
    token = await claim()
    evidence = {'repo': 'o/r', 'pr_number': 1, 'expected_head_sha': HEAD,
                'requested_base_sha': BASE, 'mechanical_kind': 'update_branch'}
    monkeypatch.setattr(update, 'request', AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await update.submit(token, evidence)
    async with owned(token) as tx:
        assert tx.operation.phase == 'uncertain'


@pytest.mark.asyncio
async def test_observer_cannot_complete_changed_provenance(ownership_db, monkeypatch):
    token = await claim()
    evidence = {'repo': 'o/r', 'pr_number': 1, 'expected_head_sha': HEAD,
                'requested_base_sha': BASE, 'mechanical_kind': 'update_branch'}
    async with owned(token) as tx:
        tx.operation.provenance = evidence
        tx.start_action()

    async def changed(repo, args):
        if '/pulls/' in args[0]:
            return proc(payload(NEXT))
        async with owned(token) as tx:
            tx.operation.provenance = {**tx.operation.provenance, 'reason': 'changed'}
        ancestor = args[0].split('/')[-1].split('...')[0]
        return proc({'status': 'ahead', 'base_commit': {'sha': ancestor},
                     'merge_base_commit': {'sha': ancestor}})

    monkeypatch.setattr(update, 'request', changed)
    assert not await update.observe(token)
    async with owned(token) as tx:
        assert tx.operation.phase == 'active'
