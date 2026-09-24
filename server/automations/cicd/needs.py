"""Workflow ``needs:`` folding — which check names are children of another job.

Pure text analysis over the repo's workflow YAML (the same texts ``waste`` reads).

A job some other job ``needs:`` is already folded into that dependent's result:
on a repo that gates merges through ONE aggregator (KinDash's ``ci`` -> the
required ``ci-gate`` check; reasonbridge's required ``CI``), the branch-protection
contexts name only the aggregator, so every child read as "advisory" to
``check_verdict`` and was proposed for graduation — 49 at a time on 2026-09-24.
The children's tier is decided by the parent; only ROOT jobs (needed by nothing)
are candidates in their own right, which is exactly the advisory-check set the
graduation feature exists for.
"""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

_TEMPLATE = re.compile(r"\$\{\{.*?\}\}")


def needed_job_names(workflows: dict[str, str]) -> frozenset[str]:
    """Display names of every job some other job ``needs:``, across all files.

    ``needs:`` references job *ids*; the check name GitHub reports is the job's
    ``name:`` when set, else the id. A templated name (``${{ matrix.shard }}``)
    is kept verbatim — ``is_folded`` matches it as a wildcard. Malformed YAML
    and non-workflow documents contribute nothing.
    """
    out: set[str] = set()
    for text in workflows.values():
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            continue
        needed_ids: set[str] = set()
        for job in jobs.values():
            needs = job.get("needs") if isinstance(job, dict) else None
            if isinstance(needs, str):
                needed_ids.add(needs)
            elif isinstance(needs, list):
                needed_ids.update(str(n) for n in needs)
        for jid in needed_ids:
            job = jobs.get(jid)
            name = job.get("name") if isinstance(job, dict) else None
            out.add(str(name) if name else str(jid))
    return frozenset(out)


@lru_cache(maxsize=4096)
def _pattern(name: str) -> re.Pattern[str]:
    # `test: server (${{ matrix.shard }}/3)` -> `test: server (.*/3)`, full match.
    return re.compile(".*".join(re.escape(p) for p in _TEMPLATE.split(name)))


def is_folded(check: str, needed: frozenset[str]) -> bool:
    """True when ``check`` — or the caller-job prefix of a reusable-workflow check
    (``<caller job> / <callee job>``) — is a needed job.

    Reusable-workflow children report under their caller's name, so a needed
    caller folds every check beneath it, at any nesting depth
    (``full-ci / static-checks / test: server (3/3)`` folds on ``full-ci``).
    """
    if not needed:
        return False
    segments = check.split(" / ")
    prefixes = [" / ".join(segments[:i]) for i in range(1, len(segments) + 1)]
    return any(_pattern(n).fullmatch(p) for n in needed for p in prefixes)
