# server/github/merge_queue.py
"""Pure helpers for the repo-level merge queue: GraphQL builder + response parser.

Mirrors the style of server/github/pr_status.py (build_*_query / parse_*_response).
No I/O — the poller runs the query via gh and feeds the response here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class QueueEntry:
    pr_number: int
    position: int | None
    state: str                 # QUEUED | AWAITING_CHECKS | MERGEABLE | UNMERGEABLE | LOCKED
    enqueued_at: int | None     # epoch seconds
    batch_head_oid: str | None  # speculative merge commit = batch identity
    group_checks: str | None    # 'passing' | 'failing' | 'pending'
    checks_total: int | None
    checks_done: int | None
    eta_seconds: int | None


def _alias(i: int) -> str:
    return f"r{i}"


def build_merge_queue_query(repos: list[str]) -> str:
    if not repos:
        return ""
    blocks = []
    for i, repo in enumerate(repos):
        owner, _, name = repo.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  mergeQueue {{ entries(first: 50) {{ nodes {{\n"
            f"    position state enqueuedAt estimatedTimeToMerge\n"
            f"    headCommit {{ oid statusCheckRollup {{ state contexts(first: 100) {{\n"
            f"      totalCount nodes {{ __typename ... on CheckRun {{ status }} ... on StatusContext {{ state }} }}\n"
            f"    }} }} }}\n"
            f"    pullRequest {{ number }}\n"
            f"  }} }} }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _group_checks(rollup_state: str | None) -> str | None:
    if rollup_state in ("SUCCESS", "EXPECTED"):
        return "passing"
    if rollup_state in ("FAILURE", "ERROR"):
        return "failing"
    if rollup_state in ("PENDING", "IN_PROGRESS", "QUEUED"):
        return "pending"
    return None


_TERMINAL_STATUS_STATES = {"SUCCESS", "FAILURE", "ERROR", "EXPECTED"}


def _checks_done(rollup: dict) -> int | None:
    """Completed-context count = the progress numerator (Task 1 spike: GitHub gives
    per-context status, not a passed-count). CheckRun done when status==COMPLETED;
    StatusContext done when its state is terminal."""
    nodes = ((rollup.get("contexts") or {}).get("nodes"))
    if not nodes:
        return None
    done = 0
    for c in nodes:
        if c.get("__typename") == "CheckRun":
            if c.get("status") == "COMPLETED":
                done += 1
        elif c.get("state") in _TERMINAL_STATUS_STATES:   # StatusContext
            done += 1
    return done


def _entry(node: dict) -> QueueEntry | None:
    pr = node.get("pullRequest") or {}
    number = pr.get("number")
    if number is None:
        return None
    head = node.get("headCommit") or {}
    rollup = head.get("statusCheckRollup") or {}    # may be null on UNMERGEABLE entries
    total = ((rollup.get("contexts") or {}).get("totalCount"))
    return QueueEntry(
        pr_number=int(number),
        position=node.get("position"),
        state=node.get("state", ""),
        enqueued_at=_epoch(node.get("enqueuedAt")),
        batch_head_oid=head.get("oid"),
        group_checks=_group_checks(rollup.get("state")),
        checks_total=total,
        checks_done=_checks_done(rollup),
        eta_seconds=node.get("estimatedTimeToMerge"),
    )


def parse_merge_queue_response(response: dict, repos: list[str]) -> dict[str, tuple[QueueEntry, ...]]:
    data = (response or {}).get("data") or {}
    out: dict[str, tuple[QueueEntry, ...]] = {}
    for i, repo in enumerate(repos):
        node = data.get(_alias(i)) or {}
        mq = node.get("mergeQueue") or {}
        nodes = ((mq.get("entries") or {}).get("nodes")) or []
        entries = tuple(e for e in (_entry(n) for n in nodes if n) if e is not None)
        out[repo] = entries
    return out
