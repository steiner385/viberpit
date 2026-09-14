"""One fenced GitHub branch-update boundary for manual and automatic recovery."""
from __future__ import annotations

import asyncio
import json
import re

from ccswitch import gitrepos
from server.automations.ownership import owned
from server.db import db_session
from server.db.models import AutomationOperation


async def request(repo: str, args: list[str]):
    """Use the canonical owner credential selector and pin the GitHub authority."""
    token = await asyncio.to_thread(gitrepos.app_token_for_owner, repo.split('/')[0])
    return await asyncio.to_thread(gitrepos._run_gh,
                                  ['api', '--hostname', 'github.com', *args],
                                  token=token, timeout=30)


def _payload(proc) -> dict:
    if proc.returncode:
        raise ValueError('GitHub read failed')
    value = json.loads(proc.stdout)
    if not isinstance(value, dict):
        raise ValueError('GitHub response is not an object')
    return value


async def snapshot(repo: str, number: int, *, require_open: bool = True, transport=None) -> dict:
    data = _payload(await (transport or request)(repo, [f'repos/{repo}/pulls/{number}']))
    head, base = data.get('head', {}), data.get('base', {})
    if (data.get('number') != number or (require_open and data.get('state') != 'open')
            or data.get('html_url', '').lower() != f'https://github.com/{repo}/pull/{number}'.lower()
            or base.get('repo', {}).get('full_name', '').lower() != repo.lower()
            or not all(isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha)
                       for sha in (head.get('sha'), base.get('sha')))):
        raise ValueError('Unverified GitHub PR snapshot')
    return {'repo': repo, 'pr_number': number, 'expected_head_sha': head['sha'],
            'requested_base_sha': base['sha'], 'mechanical_kind': 'update_branch'}


async def submit(token: str, evidence: dict, *, transport=None) -> str:
    """Persist the intended effect before sending; rc=0 proves acceptance only."""
    async with owned(token) as tx:
        tx.operation.provenance = {**tx.operation.provenance, **evidence}
        tx.start_action()
    outcome = 'unknown'
    try:
        repo, number = evidence['repo'], evidence['pr_number']
        proc = await (transport or request)(repo, ['-X', 'PUT', f'repos/{repo}/pulls/{number}/update-branch',
                                    '-f', f"expected_head_sha={evidence['expected_head_sha']}"])
        if proc.returncode == 0:
            outcome = 'accepted'
        else:
            # Only explicit synchronous HTTP refusals prove that this request
            # did not enqueue work. Timeouts, malformed replies and 5xx do not.
            body = f'{proc.stdout or ""}\n{proc.stderr or ""}'
            if re.search(r'\(HTTP (?:401|403|404|405|409|422)\)', body):
                outcome = 'completed' if 'no new commits' in body.lower() else 'rejected'
    finally:
        from server.automations.ownership import StaleOwnership
        try:
            async with owned(token) as tx:
                if tx.operation.provenance.get('mechanical_observed_completed'):
                    outcome = 'completed'
                else:
                    tx.operation.provenance = {**tx.operation.provenance, 'mechanical_outcome': outcome}
                    if outcome in ('rejected', 'completed'):
                        tx.complete_action()
                    else:
                        tx.mark_uncertain('GitHub update pending; fresh completion evidence required')
        except StaleOwnership:
            async with db_session() as db:
                op = await db.get(AutomationOperation, token)
                if not op or not op.provenance.get('mechanical_observed_completed'):
                    raise
                outcome = 'completed'
    return outcome


async def observe(token: str, *, transport=None) -> bool:
    """Prove both pinned commits are ancestors of a changed head, outside SQL locks.

    Compare responses name the exact merge base. A cached mergeable state, the
    current base branch, or elapsed time cannot establish this proof.
    """
    async with db_session() as db:
        operation = await db.get(AutomationOperation, token)
        if operation is None:
            return False
        evidence = dict(operation.provenance)
        if (operation.phase not in ('active', 'uncertain')
                or operation.session_id or operation.reserved_session_id
                or evidence.get('mechanical_kind') != 'update_branch'):
            return operation.phase == 'completed'
    try:
        repo, number = evidence['repo'], evidence['pr_number']
        head = (await snapshot(repo, number, require_open=False, transport=transport))['expected_head_sha']
        if head == evidence['expected_head_sha']:
            return False
        for ancestor in (evidence['expected_head_sha'], evidence['requested_base_sha']):
            comparison = _payload(await (transport or request)(repo, [f'repos/{repo}/compare/{ancestor}...{head}']))
            if (comparison.get('status') not in ('ahead', 'identical')
                    or comparison.get('merge_base_commit', {}).get('sha') != ancestor
                    or comparison.get('base_commit', {}).get('sha') != ancestor):
                return False
    except Exception:
        return False
    async with owned(token) as tx:
        if tx.operation.provenance != evidence or tx.operation.phase not in ('active', 'uncertain'):
            return tx.operation.phase == 'completed'
        tx.complete_action()
        tx.operation.provenance = {**tx.operation.provenance, 'mechanical_observed_completed': True}
    return True


async def release_manual(token: str) -> None:
    """Settle once, including an observer racing the original HTTP response."""
    from server.automations.ownership import ReleaseProof, StaleOwnership
    try:
        async with owned(token) as tx:
            if tx.operation.owner != 'map-changes' or tx.operation.phase != 'completed':
                raise ValueError('manual update completion has not been observed')
            tx.release(ReleaseProof.WORK_STOPPED, 'Observed completed manual branch update')
    except StaleOwnership:
        async with db_session() as db:
            op = await db.get(AutomationOperation, token)
            if (not op or op.owner != 'map-changes' or op.phase != 'released'
                    or not (op.provenance.get('mechanical_observed_completed')
                            or op.provenance.get('mechanical_outcome') in ('completed', 'rejected'))):
                raise


async def manual_update(repo: str, number: int, *, transport=None, allow_start: bool = True,
                        project_id: str | None = None) -> str:
    """Manual map recovery shares target exclusion without PR Doctor policy gates."""
    from server.automations.ownership import acquire, OwnershipConflict
    from server.automations.ownership_reconciliation import reconcile_legacy_targets
    from server.automations.target_identity import resolve_targets
    from server.db.models import AutomationOwnedTarget

    async def fetch(owner, query):
        return _payload(await (transport or request)(repo, ['graphql', '-f', f'query={query}']))

    targets = await resolve_targets([f'{repo}#{number}'], kind='pr', include_linked_issues=True,
                                    fetch=fetch if transport else None)
    async with db_session() as db:
        rows = [await db.get(AutomationOwnedTarget, t.key) for t in targets]
        tokens = {row.operation_token for row in rows if row and row.operation_token}
        operations = [await db.get(AutomationOperation, token) for token in tokens]
        resumable = [op.token for op in operations if op and op.owner == 'map-changes'
                     and op.provenance.get('repo') == repo
                     and op.provenance.get('pr_number') == number]
    for token in resumable:
        if not await observe(token, transport=transport):
            raise OwnershipConflict('GitHub branch update is still pending; completion unverified')
        await release_manual(token)
        return 'completed'
    if not allow_start:
        if tokens:
            raise OwnershipConflict('PR has a different active owner')
        return 'completed'  # Concurrent observer already released the pending request.
    evidence = await snapshot(repo, number, transport=transport)
    await reconcile_legacy_targets(targets)
    async with acquire(targets, owner='map-changes', source='manual-rebase',
                       reason='Manual map branch update requested',
                       provenance={'project_id': project_id, 'repo': repo, 'pr_number': number}) as tx:
        token = tx.token
    outcome = await submit(token, evidence, transport=transport)
    if outcome in ('rejected', 'completed'):
        await release_manual(token)
    return outcome
