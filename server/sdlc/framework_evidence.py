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


def review_observation(snapshot: FrameworkPolicySnapshot, entry: dict, concern: str) -> str:
    """Validate an explicit attestation, not its substantive conclusions or usage."""
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
        if not snapshot.author or login.casefold() == snapshot.author.casefold():
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
    for principal, (_, _, review, record) in latest.items():
        if principal in invalid_order or principal in latest_tied:
            continue
        try:
            if review.get('state') not in {'COMMENTED', 'APPROVED'} or review.get('isMinimized', False):
                continue
            editor = (review.get('editor') or {}).get('login')
            login = review['author']['login']
            if (review.get('lastEditedAt') or editor is not None) and (not isinstance(editor, str) or editor.casefold() != login.casefold()):
                continue
            if (review.get('commit') or {}).get('oid') != snapshot.head_sha:
                continue
            bindings = {'version': 1, 'framework': entry['id'], 'concern': concern,
                        'repo': snapshot.repo, 'base_sha': snapshot.base_sha, 'head_sha': snapshot.head_sha,
                        'framework_digest': snapshot.framework_digest, 'policy_digest': snapshot.policy_digest,
                        'scope_digest': policy.scope_digest(snapshot.changed_paths)}
            if (set(record) != set(bindings) | {'artifacts', 'findings'} or type(record.get('version')) is not int
                    or any(record.get(key) != value or value is None for key, value in bindings.items())):
                continue
            artifacts = record['artifacts']
            if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 20:
                continue
            if any(not isinstance(a, dict) or set(a) != {'path', 'sha256'} or not policy.safe_path(a['path'])
                   or not isinstance(a['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', a['sha256']) for a in artifacts):
                continue
            findings = record['findings']
            if not isinstance(findings, list) or len(findings) > 100:
                continue
            if any(not isinstance(f, dict) or set(f) != {'id', 'disposition'}
                   or not isinstance(f['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', f['id'])
                   or f['disposition'] not in {'fixed', 'accepted', 'not_applicable'} for f in findings):
                continue
            if len({f['id'] for f in findings}) != len(findings):
                continue
            return 'review_recorded'
        except (KeyError, TypeError, ValueError):
            continue
    return 'missing' if not latest else 'invalid'
