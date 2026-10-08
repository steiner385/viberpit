"""Pure authenticated-review validation; never authorizes SDLC transitions."""
from __future__ import annotations
import json
import re
from typing import TYPE_CHECKING
from server.sdlc.framework_policy import policy

if TYPE_CHECKING:
    from server.sdlc.gates import FrameworkPolicySnapshot


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _record_problem(review: dict, record: dict, entry: dict, concern: str,
                    snapshot: FrameworkPolicySnapshot) -> str | None:
    """None for a recorded review; ``stale`` for a well-formed record bound to
    an older head/base/declaration/scope; ``withdrawn`` for a dismissed or
    minimized record; ``invalid`` for anything malformed, objecting or edited
    by someone else. Stale and withdrawn are an absence of current evidence;
    invalid is a trusted reviewer's finding to fix."""
    try:
        # A dismissed or hidden record was withdrawn by a person: it is absent
        # evidence (waivable), not a reviewer's objection to fix.
        if review.get('state') == 'DISMISSED' or review.get('isMinimized', False):
            return 'withdrawn'
        if review.get('state') not in {'COMMENTED', 'APPROVED'}:
            return 'invalid'
        editor = (review.get('editor') or {}).get('login')
        login = review['author']['login']
        if (review.get('lastEditedAt') or editor is not None) and (not isinstance(editor, str) or editor.casefold() != login.casefold()):
            return 'invalid'
        bindings = {'version': 1, 'framework': entry['id'], 'concern': concern, 'repo': snapshot.repo}
        moving = {'base_sha': snapshot.base_sha, 'head_sha': snapshot.head_sha,
                  'framework_digest': snapshot.framework_digest, 'policy_digest': snapshot.policy_digest,
                  'scope_digest': policy.scope_digest(snapshot.changed_paths)}
        if (set(record) != set(bindings) | set(moving) | {'artifacts', 'findings'} or type(record.get('version')) is not int
                or any(record.get(key) != value for key, value in bindings.items())):
            return 'invalid'
        artifacts = record['artifacts']
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 20:
            return 'invalid'
        if any(not isinstance(a, dict) or set(a) != {'path', 'sha256'} or not policy.safe_path(a['path'])
               or not isinstance(a['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', a['sha256']) for a in artifacts):
            return 'invalid'
        findings = record['findings']
        if not isinstance(findings, list) or len(findings) > 100:
            return 'invalid'
        if any(not isinstance(f, dict) or set(f) != {'id', 'disposition'}
               or not isinstance(f['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', f['id'])
               or f['disposition'] not in {'fixed', 'accepted', 'not_applicable'} for f in findings):
            return 'invalid'
        if len({f['id'] for f in findings}) != len(findings):
            return 'invalid'
        if any(not isinstance(record[key], str) for key in moving):
            return 'invalid'
        if (review.get('commit') or {}).get('oid') != snapshot.head_sha:
            return 'stale'
        if any(value is None or record[key] != value for key, value in moving.items()):
            return 'stale'
        return None
    except (KeyError, TypeError, ValueError):
        return 'invalid'


def review_observation(snapshot: FrameworkPolicySnapshot, entry: dict, concern: str) -> str:
    """Validate an explicit attestation, not its substantive conclusions or usage.

    ``review_recorded`` > ``invalid`` > ``stale`` > ``missing`` across trusted
    principals: one malformed record from a trusted reviewer is not excused by
    another's merely outdated one."""
    from datetime import datetime
    if not snapshot.reviews_complete or not snapshot.scope_complete or not snapshot.changed_paths:
        return "missing"
    if entry['state'] in {'rejected', 'revoked'}:
        return "ineligible"
    trusted = {login.casefold() for login in entry['observation']['trusted_github_reviewers']}
    invalid_order: set[str] = set()
    latest_tied: set[str] = set()
    latest: dict[str, tuple[datetime, str, dict, dict]] = {}
    for review in snapshot.reviews:
        body = review.get('body')
        login = (review.get('author') or {}).get('login')
        if not isinstance(login, str) or login.casefold() not in trusted:
            continue
        if not snapshot.author or policy.same_principal(login, snapshot.author):
            continue
        if not isinstance(body, str) or len(body) > 65536:
            invalid_order.add(login.casefold())
            continue
        if '```viberpit-framework-review' not in body:
            continue
        match = re.fullmatch(r'```viberpit-framework-review\n(.*?)\n```', body.strip(), re.DOTALL)
        # Only a whole-body record is intentional. Invalid/multiple records
        # supersede this principal's earlier success conservatively.
        record = {}
        if match:
            try:
                record = json.loads(match.group(1), object_pairs_hook=_unique_object)
            except (ValueError, RecursionError):
                pass
        if not isinstance(record, dict):
            record = {}
        if any(key in record and isinstance(record[key], str) and record[key] != value
               for key, value in (('framework', entry['id']), ('concern', concern))):
            continue
        try:
            stamp = datetime.fromisoformat(review['submittedAt'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or not isinstance(review.get('id'), str):
                invalid_order.add(login.casefold())
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid_order.add(login.casefold())
            continue
        key = login.casefold()
        candidate = (stamp, review['id'], review, record)
        if key in latest and stamp == latest[key][0]:
            # GitHub IDs are opaque, not chronological counters. A tie cannot
            # establish which relevant record supersedes the other.
            latest_tied.add(key)
        if key not in latest or stamp > latest[key][0]:
            latest[key] = candidate
            latest_tied.discard(key)
    problems = {'invalid'} if invalid_order else set()
    for principal, (_, _, review, record) in latest.items():
        if principal in invalid_order or principal in latest_tied:
            problems.add('invalid')
            continue
        problem = _record_problem(review, record, entry, concern, snapshot)
        if problem is None:
            return 'review_recorded'
        if problem != 'withdrawn':
            problems.add(problem)
    for status in ('invalid', 'stale'):
        if status in problems:
            return status
    return 'missing'
