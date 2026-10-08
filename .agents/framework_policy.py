"""Portable declaration policy; stdlib only, no execution of target repository code."""
from __future__ import annotations
from datetime import datetime
from fnmatch import fnmatchcase
import hashlib
import json
import re

TIERS = {'deterministic', 'required_review', 'advisory'}
STATES = {'candidate', 'sandbox', 'pilot', 'approved', 'rejected', 'revoked'}
# Ordered weakest first. ``observe`` never blocks; ``evidence_required`` blocks
# on missing evidence for applicable scope; ``enforced`` also blocks unknown scope.
MODES = ('observe', 'evidence_required', 'enforced')
_LOGIN = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?'
_EXCEPTION_KEYS = {'concern', 'pull_request', 'owner', 'reason', 'expires_at', 'compensating_evidence'}


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def framework_digest(lock):
    return canonical_digest(dict(lock, frameworks=sorted(lock['frameworks'], key=lambda item: item['id'])))


def scope_digest(paths):
    return canonical_digest(sorted(set(paths)))


def safe_path(value, *, pattern=False):
    chars = r'[A-Za-z0-9_./*?-]+' if pattern else r'[A-Za-z0-9_./-]+'
    return (isinstance(value, str) and 0 < len(value) <= 512 and re.fullmatch(chars, value)
            and not value.startswith('/') and all(p not in {'', '.', '..'} for p in value.split('/')))


def parse_expiry(value):
    """Timezone-aware expiry; a naive time has no unambiguous instant to expire at."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid exception expiry')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('invalid exception expiry')
    return stamp


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 1000 and value == value.strip()


def validate_exceptions(observation, concerns):
    exceptions = observation.get('exceptions', [])
    if not isinstance(exceptions, list) or len(exceptions) > 50:
        raise ValueError('invalid observation exceptions')
    seen = set()
    for item in exceptions:
        if (not isinstance(item, dict) or set(item) != _EXCEPTION_KEYS or item['concern'] not in concerns
                or type(item['pull_request']) is not int or item['pull_request'] <= 0
                or not isinstance(item['owner'], str) or not re.fullmatch(_LOGIN, item['owner'])
                or not _text(item['reason']) or not _text(item['compensating_evidence'])):
            raise ValueError('invalid observation exception')
        parse_expiry(item['expires_at'])
        key = (item['concern'], item['pull_request'])
        if key in seen:
            raise ValueError('duplicate observation exception')
        seen.add(key)


def validate_observation(entry):
    observation = entry.get('observation')
    if observation is None:
        return
    required = {'mode', 'trusted_github_reviewers', 'concerns'}
    if (not isinstance(observation, dict) or not required <= set(observation) <= required | {'exceptions'}
            or observation['mode'] not in MODES):
        raise ValueError('invalid observation policy')
    reviewers = observation['trusted_github_reviewers']
    if (not isinstance(reviewers, list) or len(reviewers) > 100
            or any(not isinstance(v, str) or not re.fullmatch(_LOGIN, v) for v in reviewers)
            or len({v.casefold() for v in reviewers}) != len(reviewers)):
        raise ValueError('invalid trusted GitHub reviewers')
    concerns = observation['concerns']
    if not isinstance(concerns, dict) or not concerns or not set(concerns) <= set(entry['concerns']):
        raise ValueError('invalid observation concerns')
    for rule in concerns.values():
        keys = {'tier', 'include_paths', 'exclude_paths'}
        if (not isinstance(rule, dict) or not keys <= set(rule) <= keys | {'mode'} or rule['tier'] not in TIERS
                or rule.get('mode', 'observe') not in MODES):
            raise ValueError('invalid observation rule')
        for key in ('include_paths', 'exclude_paths'):
            paths = rule[key]
            if not isinstance(paths, list) or len(paths) > 100 or any(not safe_path(p, pattern=True) for p in paths) or len(set(paths)) != len(paths):
                raise ValueError('invalid observation paths')
    validate_exceptions(observation, concerns)


def same_principal(left, right):
    """Whether two GitHub logins name one principal. GraphQL reports bot authors
    without the ``[bot]`` suffix that REST and configuration use, so strip it
    before comparing. Only for conservative author checks (self-review,
    self-issued exceptions); allowlist membership stays exact."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.casefold().removesuffix('[bot]') == right.casefold().removesuffix('[bot]')


def effective_mode(entry, concern):
    """A concern's own mode overrides the entry default; unconfigured is observe."""
    observation = entry.get('observation') or {}
    rule = observation.get('concerns', {}).get(concern)
    if rule is None:
        return 'observe'
    return rule.get('mode', observation['mode'])


def validate_declarations(lock):
    if not isinstance(lock, dict) or type(lock.get('schema_version')) is not int or lock['schema_version'] != 1 or not isinstance(lock.get('frameworks'), list):
        raise ValueError('unsupported framework lock schema')
    ids, concerns = set(), set()
    for entry in lock['frameworks']:
        if not isinstance(entry, dict):
            raise ValueError('framework entry must be an object')
        name = entry.get('id')
        if not isinstance(name, str) or not name.strip() or name != name.strip() or name in ids:
            raise ValueError('missing or duplicate framework id')
        ids.add(name)
        if not isinstance(entry.get('state'), str) or entry['state'] not in STATES:
            raise ValueError('invalid admission state')
        values = entry.get('concerns')
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values) or len(set(values)) != len(values):
            raise ValueError('concerns must be unique nonempty strings')
        if concerns.intersection(values):
            raise ValueError('duplicate concern ownership')
        concerns.update(values)
        validate_observation(entry)


def _matches(path, pattern):
    # Dynamic programming across segments; fnmatchcase uses bounded atomic-star
    # matching within a segment. Never compile caller-authored regex expressions.
    parts = path.split('/')
    previous = [True] + [False] * len(parts)
    for chunk in pattern.split('/'):
        current = [previous[0] if chunk == '**' else False]
        for index, part in enumerate(parts, 1):
            current.append((previous[index] or current[index-1]) if chunk == '**'
                           else previous[index-1] and fnmatchcase(part, chunk))
        previous = current
    return previous[-1]


def applicability(rule, paths, complete):
    if any(_matches(path, pattern) for path in paths for pattern in rule['include_paths']):
        return 'applicable'
    if complete and paths and all(any(_matches(path, pattern) for pattern in rule['exclude_paths']) for path in paths):
        return 'not_applicable'
    return 'unknown'
