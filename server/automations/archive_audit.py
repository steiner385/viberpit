"""Retro audit: re-judge already-archived sessions against the current completion
bar (completion_bar.met_bar — the SAME predicate the live autopilot uses). Reports
every session and auto-unarchives the confidently-premature ones. Every I/O
dependency (gh, unarchive) is injected, so the pipeline is unit-tested with no
network."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .completion_bar import BarVerdict, PrFact, PrFacts, met_bar

log = logging.getLogger(__name__)

GhFn = Callable[[str, str], list[dict]]              # (repo, branch) -> raw PR dicts
UnarchiveFn = Callable[[str], Awaitable[None] | None]


def _pr_fact_from_gh(d: dict) -> PrFact:
    """Map one `gh pr list --json ...` row to a PrFact. gh states are UPPERCASE
    (OPEN/MERGED/CLOSED); autoMergeRequest is non-null when auto-merge is armed."""
    state = str(d.get("state", "")).lower()
    is_draft = bool(d.get("isDraft", False))
    if state == "open" and is_draft:
        state = "draft"
    return PrFact(number=int(d.get("number", 0)), state=state, is_draft=is_draft,
                  auto_merge_enabled=d.get("autoMergeRequest") is not None,
                  url=str(d.get("url", "")))


def build_pr_facts(repo: str | None, branches: list[str], gh_fn: GhFn) -> PrFacts:
    """PrFacts for an archived session by querying real PRs per branch. A raising
    gh_fn propagates (the caller buckets that session ambiguous)."""
    seen: dict[int, PrFact] = {}
    for br in branches:
        if not repo or not br:
            continue
        for raw in gh_fn(repo, br) or []:
            fact = _pr_fact_from_gh(raw)
            seen.setdefault(fact.number, fact)
    return PrFacts(has_repo=bool(repo), prs=tuple(seen.values()))


def classify(goal: str, goal_source: str, facts: PrFacts) -> tuple[str, BarVerdict]:
    """Bucket via met_bar: met→legit; not-met+high→premature; not-met+low→ambiguous."""
    v = met_bar(goal, goal_source, facts)
    if v.met:
        return "legit", v
    return ("premature" if v.confidence == "high" else "ambiguous"), v


async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        return await x
    return x


async def run_audit(rows: list[dict], gh_fn: GhFn, unarchive_fn: UnarchiveFn,
                    *, dry_run: bool = False) -> list[dict]:
    """Audit each archived-session row; auto-unarchive the premature ones (unless
    dry_run). Best-effort: one failing session never aborts the run."""
    results: list[dict] = []
    for row in rows:
        sid = row["id"]
        branches = list(row.get("authored_branches") or [])
        if row.get("branch"):
            branches.append(row["branch"])
        try:
            facts = build_pr_facts(row.get("repo"), branches, gh_fn)
        except Exception as exc:
            log.warning("archive_audit gh failed sid=%s: %s", sid, exc)
            results.append({"id": sid, "bucket": "ambiguous", "reason": f"gh error: {exc}",
                            "goal": row.get("goal", ""), "action": "none"})
            continue
        bucket, verdict = classify(row.get("goal", ""), row.get("goal_source", ""), facts)
        action = "none"
        if bucket == "premature" and not dry_run:
            try:
                await _maybe_await(unarchive_fn(sid))
                action = "unarchived"
            except Exception as exc:  # unarchive failure never aborts the audit
                log.warning("archive_audit unarchive failed sid=%s: %s", sid, exc)
                action = f"unarchive-failed: {exc}"
        elif bucket == "premature":
            action = "would-unarchive"
        results.append({"id": sid, "bucket": bucket, "reason": verdict.reason,
                        "goal": row.get("goal", ""), "action": action})
    return results


def format_report(results: list[dict]) -> str:
    """Human-readable table + counts."""
    from collections import Counter
    counts = Counter(r["bucket"] for r in results)
    lines = [f"Archive audit: {len(results)} sessions — "
             + ", ".join(f"{k}={counts[k]}" for k in ("legit", "premature", "ambiguous"))]
    for r in results:
        lines.append(f"  [{r['bucket']:<9}] {r['id'][:8]}  {r['action']:<16} "
                     f"{r['goal'][:48]!r}  — {r['reason']}")
    return "\n".join(lines)
