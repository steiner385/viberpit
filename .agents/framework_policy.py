"""Portable declaration policy; stdlib only, no execution of target repository code."""
from __future__ import annotations
from fnmatch import fnmatchcase
import hashlib
import json
import re

TIERS = {'deterministic', 'required_review', 'advisory'}
STATES = {'candidate', 'sandbox', 'pilot', 'approved', 'rejected', 'revoked'}


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


def validate_observation(entry):
    observation = entry.get('observation')
    if observation is None:
        return
    if not isinstance(observation, dict) or set(observation) != {'mode', 'trusted_github_reviewers', 'concerns'} or observation['mode'] != 'observe':
        raise ValueError('invalid observation policy')
    reviewers = observation['trusted_github_reviewers']
    if (not isinstance(reviewers, list) or len(reviewers) > 100
            or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?', v) for v in reviewers)
            or len({v.casefold() for v in reviewers}) != len(reviewers)):
        raise ValueError('invalid trusted GitHub reviewers')
    concerns = observation['concerns']
    if not isinstance(concerns, dict) or not concerns or not set(concerns) <= set(entry['concerns']):
        raise ValueError('invalid observation concerns')
    for rule in concerns.values():
        if not isinstance(rule, dict) or set(rule) != {'tier', 'include_paths', 'exclude_paths'} or rule['tier'] not in TIERS:
            raise ValueError('invalid observation rule')
        for key in ('include_paths', 'exclude_paths'):
            paths = rule[key]
            if not isinstance(paths, list) or len(paths) > 100 or any(not safe_path(p, pattern=True) for p in paths) or len(set(paths)) != len(paths):
                raise ValueError('invalid observation paths')


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
