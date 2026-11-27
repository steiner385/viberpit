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

from server.github.deploy_envs import EnvCategory, classify_env


def env_categories_present(prs: Sequence[Any]) -> set[EnvCategory]:
    """The deploy categories a repo is tracked in: those at least one merged PR
    in the window is live in (``backlog_from_prs``'s "present" rule)."""
    present: set[EnvCategory] = set()
    for pr in prs:
        if getattr(pr, "merged", False) or pr.state == "merged":
            present |= {c for e in pr.deployed_envs if (c := classify_env(e))}
    return present


def awaiting_categories(pr: Any, present: set[EnvCategory]) -> set[EnvCategory]:
    """The tracked categories merged ``pr`` is not live in yet — THE per-PR rule
    behind both ``backlog_from_prs`` and the board's deploy stage."""
    live = {c for e in pr.deployed_envs if (c := classify_env(e))}
    return present - live


def deploy_wait(
    merged_at: str | None,
    qa_live_at: str | None,
    prod_live_at: str | None,
    newest_prod_merged_at: str | None,
) -> str | None:
    """What a configured repo's merged PR waits on: ``'qa'``, ``'prod'`` or None.

    THE rule behind ``compute_repo_deploy``'s awaitingQa/awaitingProd and the
    board's qa-deploy/awaiting-prod stages, so the deploy block and the Deploying
    tile count the same PRs. None = live in prod, or superseded (#205): merged
    before the newest prod-live merge, so it rolled out with it.
    """
    if prod_live_at:
        return None
    if newest_prod_merged_at and merged_at and merged_at < newest_prod_merged_at:
        return None
    return "prod" if qa_live_at else "qa"


def backlog_from_prs(prs: Sequence[Any]) -> dict | None:
    """Per-repo qa/prod/admin deploy backlog from GitHub-Deployments data.

    ``prs``: the repo's PRs this frame (each needs ``.merged``/``.state`` and
    ``.deployed_envs``). Only merged PRs count. ``deployed_envs`` already encodes
    date-based liveness (an env is present iff its newest successful deploy is
    at-or-after the PR's merge), so a PR is "awaiting <category>" exactly when no
    env of that category is among its deployed_envs, and merges older than the
    latest deploy are counted as done (supersession for free).

    A category is "present" (tracked) only when at least one merged PR in the
    window is live in it — so a repo that never deploys to a category shows 0
    there, and a repo with no classifiable deploy env at all returns ``None``
    (no backlog line). Always returns all three keys when non-None.
    """
    merged = [p for p in prs if getattr(p, "merged", False) or p.state == "merged"]
    present = env_categories_present(merged)
    if not present:
        return None
    counts = {"qa": 0, "prod": 0, "admin": 0}
    for pr in merged:
        for cat in awaiting_categories(pr, present):
            counts[cat] += 1
    return {"awaitingQa": counts["qa"], "awaitingProd": counts["prod"],
            "awaitingAdmin": counts["admin"]}


def compute_repo_deploy(
    merges: Sequence[Any], envs: list[dict], chain_limit: int = 8
) -> dict:
    """plotroom deploy-status.ts computeRepoDeploy + buildChain (SPEC §3).

    ``merges``: MergedPr rows in the retention window, any order.
    """
    ordered = sorted(merges, key=lambda m: m.merged_at, reverse=True)
    newest_prod = next((m for m in ordered if m.prod_live_at), None)
    newest_prod_at = newest_prod.merged_at if newest_prod else None
    awaiting_qa = awaiting_prod = 0
    for m in ordered:
        wait = deploy_wait(m.merged_at, m.qa_live_at, m.prod_live_at, newest_prod_at)
        if wait == "prod":
            awaiting_prod += 1
        elif wait == "qa":
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
            # The supersession cut the board's per-PR deploy stage applies too
            # (dashboard_state), so the Deploying tile matches these counts.
            "newestProdMergedAt": newest_prod_at,
            "chain": {"entries": entries, "inFlight": in_flight,
                      "supersededCount": sum(1 for e in entries if e["superseded"])}}
