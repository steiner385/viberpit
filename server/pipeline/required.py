"""Port of Plotroom's required-checks + expectedSet glue (classify.ts + poller.ts).

When a HistoryStore is passed to effective_prefixes() / rollup_workflow_for(),
those functions read the Plotroom-derived ciGraph (BFS closure of the rollup
job's ``needs:`` in ci.yml) from the migrated meta table.  Without it, repos
that gate merges on a single rollup job (``ci``) would produce a collapsed
expected-set containing only the rollup itself — matching zero history names
and yielding eta=0 even when sub-checks are still running.

Repos without a ciGraph entry fall back to the no-config path (prefixes=[],
rollup_wf=None) — valid for repos that mark individual checks isRequired in
GitHub branch protection rather than using a rollup job.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.pipeline.history import HistoryStore

EXPECTED_SET_STALE_MS = 10 * 60_000


def matching_prefix(name: str, prefixes) -> str | None:
    best = None
    for p in prefixes:
        if name.startswith(p) and (best is None or len(p) > len(best)):
            best = p
    return best


def matches_required_prefix(name: str, prefixes) -> bool:
    return prefixes is not None and matching_prefix(name, prefixes) is not None


def workflow_scope_allows(check_wf: str | None, rollup_wf: str | None = None) -> bool:
    return rollup_wf is None or check_wf is None or check_wf == rollup_wf


def required_checks(checks: list[dict], prefixes=None, rollup_wf: str | None = None) -> list[dict]:
    pr_event = [c for c in checks if c.get("event") != "merge_group"]
    req = [c for c in pr_event if c.get("isRequired")
           or (matches_required_prefix(c["name"], prefixes)
               and workflow_scope_allows(c.get("workflowName"), rollup_wf))]
    if req:
        return req
    return [] if (prefixes and len(prefixes)) else pr_event


def effective_prefixes(repo: str, store: "HistoryStore | None" = None) -> list[str]:
    """Required-check name prefixes for a repo.

    Reads the Plotroom-derived ciGraph from the migrated history store when
    available; falls back to [] for repos with no ciGraph entry.
    """
    if store is not None:
        graph = store.ci_graph(repo)
        if graph:
            return graph.get("prefixes") or []
    return []


def rollup_workflow_for(repo: str, store: "HistoryStore | None" = None) -> str | None:
    """Rollup workflow display name for a repo (e.g. ``'CI'``), or None.

    Reads the Plotroom-derived ciGraph from the migrated history store when
    available; falls back to None for repos with no ciGraph entry.
    """
    if store is not None:
        graph = store.ci_graph(repo)
        if graph:
            return graph.get("workflowName") or None
    return None


def build_expected_set(history_set: list[str], checks: list[dict], prefixes,
                       rollup_wf: str | None, now: datetime) -> list[str]:
    if prefixes:
        req_names = {c["name"] for c in required_checks(checks, prefixes, rollup_wf)}
        foreign = {c["name"] for c in checks if not workflow_scope_allows(c.get("workflowName"), rollup_wf)}
        return [n for n in history_set
                if (matches_required_prefix(n, prefixes) and n not in foreign) or n in req_names]
    if any(c.get("isRequired") for c in checks):
        req_names = {c["name"] for c in required_checks(checks, prefixes, rollup_wf)}
        return [n for n in history_set if n in req_names]
    # all-done-stale path: drop expected names no longer live once everything finished long ago
    if checks and all(c.get("status") == "COMPLETED" for c in checks):
        newest = max((_epoch_ms(c.get("completed_at")) for c in checks), default=0.0)
        if now.timestamp() * 1000.0 - newest > EXPECTED_SET_STALE_MS:
            live = {c["name"] for c in checks}
            return [n for n in history_set if n in live]
    return history_set


def _epoch_ms(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return 0.0
