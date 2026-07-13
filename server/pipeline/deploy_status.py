"""Deploy status + supersession chain (SPEC §3 — DEPLOY STATUS & CHAIN).

Pure port of plotroom ``server/estimator/deploy-status.ts`` computeRepoDeploy +
buildChain. Given the retention-window merged-PR rows for one repo and its
env-health snapshot, derive:

- ``awaitingQa`` / ``awaitingProd`` — DISJOINT buckets of not-yet-prod merges,
  with supersession (#205) excluding merges rolled into a newer prod deploy.
- ``chain`` — up to ``chain_limit`` newest-first entries with per-entry stage +
  superseded flag, the single ``inFlight`` entry, and the superseded count.

Rows need only ``.number``, ``.merged_at``, ``.qa_live_at``, ``.prod_live_at``,
``.merge_commit_sha`` — MergedPr ORM rows satisfy this. No DB / network here.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def compute_repo_deploy(
    merges: Sequence[Any], envs: list[dict], chain_limit: int = 8
) -> dict:
    """plotroom deploy-status.ts computeRepoDeploy + buildChain (SPEC §3).

    ``merges``: MergedPr rows in the retention window, any order.
    """
    ordered = sorted(merges, key=lambda m: m.merged_at, reverse=True)
    newest_prod = next((m for m in ordered if m.prod_live_at), None)
    awaiting_qa = awaiting_prod = 0
    for m in ordered:
        if m.prod_live_at:
            continue
        if newest_prod and m.merged_at < newest_prod.merged_at:
            continue                        # superseded (#205): rolled into a newer prod deploy
        if m.qa_live_at:
            awaiting_prod += 1
        else:
            awaiting_qa += 1
    entries = []
    for m in ordered[:chain_limit]:
        stage = "prod" if m.prod_live_at else "qa" if m.qa_live_at else "merged"
        superseded = (not m.prod_live_at and newest_prod is not None
                      and newest_prod.merged_at > m.merged_at)
        entries.append({"prNumber": m.number, "sha": m.merge_commit_sha,
                        "mergedAt": m.merged_at, "stage": stage,
                        "qaLiveAt": m.qa_live_at, "prodLiveAt": m.prod_live_at,
                        "superseded": superseded})
    in_flight = next(({"prNumber": e["prNumber"], "sha": e["sha"], "stage": e["stage"]}
                      for e in entries if e["stage"] != "prod" and not e["superseded"]), None)
    return {"envs": envs, "awaitingQa": awaiting_qa, "awaitingProd": awaiting_prod,
            "chain": {"entries": entries, "inFlight": in_flight,
                      "supersededCount": sum(1 for e in entries if e["superseded"])}}
