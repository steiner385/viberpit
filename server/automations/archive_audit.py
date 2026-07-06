"""Retro audit: re-judge already-archived sessions against the current completion
bar (completion_bar.met_bar — the SAME predicate the live autopilot uses). Reports
every session and auto-unarchives the confidently-premature ones. Every I/O
dependency (gh, unarchive) is injected, so the pipeline is unit-tested with no
network."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Awaitable, Callable

from .completion_bar import BarVerdict, PrFact, PrFacts, _is_code_goal, met_bar

log = logging.getLogger(__name__)

GhFn = Callable[[str, str], list[dict]]              # (repo, branch) -> raw PR dicts
GhNumFn = Callable[[str, int], "dict | None"]        # (repo, number) -> raw PR dict or None
UnarchiveFn = Callable[[str], Awaitable[None] | None]

# Matches "#N" or "PR N" / "PR #N" — 2-6 digit numbers only.
_REF_GOAL_RE = re.compile(r"(?:PR\s*#?|#)(\d{2,6})", re.IGNORECASE)
# Matches only the "your pull request ... #N" phrase in initial_prompt.
_REF_PROMPT_RE = re.compile(r"your pull request[^\n#]*#(\d{2,6})", re.IGNORECASE)


def referenced_pr_numbers(goal: str, initial_prompt: str | None) -> list[int]:
    """Extract referenced PR numbers from a goal string and optional initial_prompt.

    From goal: every #N or PR N (2–6 digit numbers).
    From initial_prompt: only the session's own deliverable, matched by
    "your pull request ... #N" — input-list numbers are NOT extracted.
    Returns a sorted, de-duplicated list of ints.
    """
    nums: set[int] = set()
    for m in _REF_GOAL_RE.finditer(goal or ""):
        nums.add(int(m.group(1)))
    for m in _REF_PROMPT_RE.finditer(initial_prompt or ""):
        nums.add(int(m.group(1)))
    return sorted(nums)


def _pr_fact_from_gh(d: dict) -> PrFact:
    """Map one `gh pr list --json ...` row to a PrFact. gh states are UPPERCASE
    (OPEN/MERGED/CLOSED); autoMergeRequest is non-null when auto-merge is armed.

    Limitation: `gh pr list/view --json` does not expose mergeQueueEntry, so
    in_merge_queue is always False here — a queued PR may be misclassified as
    open-unarmed by the retro audit. Needs a GraphQL path (tracked on #501).
    """
    state = str(d.get("state", "")).lower()
    is_draft = bool(d.get("isDraft", False))
    if state == "open" and is_draft:
        state = "draft"
    return PrFact(number=int(d.get("number", 0)), state=state, is_draft=is_draft,
                  auto_merge_enabled=d.get("autoMergeRequest") is not None,
                  url=str(d.get("url", "")))


def build_pr_facts(
    repo: str | None,
    branches: list[str],
    gh_fn: GhFn,
    *,
    ref_numbers: "list[int] | tuple[int, ...]" = (),
    gh_num_fn: "GhNumFn | None" = None,
) -> PrFacts:
    """PrFacts for an archived session by querying real PRs per branch and by
    referenced PR numbers. A raising gh_fn propagates (the caller buckets that
    session ambiguous). gh_num_fn failures are swallowed — best-effort."""
    seen: dict[int, PrFact] = {}
    for br in branches:
        if not repo or not br:
            continue
        for raw in gh_fn(repo, br) or []:
            fact = _pr_fact_from_gh(raw)
            seen.setdefault(fact.number, fact)
    if gh_num_fn is not None and repo:
        for n in ref_numbers:
            if n in seen:
                continue  # dedupe: already found via branch head
            try:
                raw_num: "dict | None" = gh_num_fn(repo, n)
            except Exception as exc:
                log.warning("archive_audit gh_num_fn failed repo=%s number=%s: %s", repo, n, exc)
                continue
            if raw_num is not None:
                fact = _pr_fact_from_gh(raw_num)
                seen.setdefault(fact.number, fact)
    return PrFacts(has_repo=bool(repo), prs=tuple(seen.values()))


def classify(goal: str, goal_source: str, facts: PrFacts) -> tuple[str, BarVerdict]:
    """Bucket via met_bar: met→legit; not-met+open-unarmed→premature;
    not-met+closed-target+code-goal→review; not-met+low→ambiguous."""
    v = met_bar(goal, goal_source, facts)
    if v.met:
        return "legit", v
    if v.confidence == "high":
        # An open-unarmed PR exists — premature wins over review.
        return "premature", v
    # Low-confidence: check for closed-unmerged evidence before falling to ambiguous.
    if _is_code_goal(goal) and any(p.state == "closed" for p in facts.prs):
        return "review", v
    return "ambiguous", v


async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        return await x
    return x


async def run_audit(
    rows: list[dict],
    gh_fn: GhFn,
    unarchive_fn: UnarchiveFn,
    *,
    dry_run: bool = False,
    gh_num_fn: "GhNumFn | None" = None,
) -> list[dict]:
    """Audit each archived-session row; auto-unarchive the premature ones (unless
    dry_run). Best-effort: one failing session never aborts the run."""
    results: list[dict] = []
    for row in rows:
        sid = row["id"]
        branches = list(row.get("authored_branches") or [])
        if row.get("branch"):
            branches.append(row["branch"])
        ref_numbers = referenced_pr_numbers(row.get("goal", ""), row.get("initial_prompt"))
        try:
            facts = build_pr_facts(
                row.get("repo"), branches, gh_fn,
                ref_numbers=ref_numbers, gh_num_fn=gh_num_fn,
            )
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
             + ", ".join(f"{k}={counts[k]}" for k in ("legit", "premature", "ambiguous", "review"))]
    for r in results:
        lines.append(f"  [{r['bucket']:<9}] {r['id'][:8]}  {r['action']:<16} "
                     f"{r['goal'][:48]!r}  — {r['reason']}")
    return "\n".join(lines)


def _real_gh(repo: str, branch: str) -> list[dict]:
    """`gh pr list` for one head branch, all states. Returns [] on any gh failure —
    the caller then buckets the session ambiguous (never a false 'premature')."""
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
             "--json", "number,state,isDraft,autoMergeRequest,url"],
            capture_output=True, text=True, timeout=30, check=True)
    except Exception as exc:  # gh missing / auth / network / non-zero
        log.warning("archive_audit gh pr list failed repo=%s head=%s: %s", repo, branch, exc)
        return []
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _real_gh_num(repo: str, number: int) -> "dict | None":
    """`gh pr view <number>` for a single PR by number. Returns None on ANY gh
    failure (missing/auth/network/non-zero/JSON error) — never raises."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo,
             "--json", "number,state,isDraft,autoMergeRequest,url"],
            capture_output=True, text=True, timeout=30, check=True)
    except Exception as exc:
        log.warning("archive_audit gh pr view failed repo=%s number=%s: %s", repo, number, exc)
        return None
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None


async def load_archived_rows(session_factory) -> list[dict]:
    """Load archived Session rows with the fields the audit needs (the list_archived
    seam omits repo/branch/authored_branches/goal, so read the model directly).
    session_factory() is an async context manager yielding a DB session."""
    from sqlalchemy import select as sa_select
    from server.db.models import Session as DbSession
    from server.sessions.goals import effective_goal
    async with session_factory() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(DbSession.status == "archived"))).scalars().all()
    out: list[dict] = []
    for row in rows:
        goal, source = effective_goal(row)
        out.append({"id": row.id, "repo": row.repo, "branch": row.branch,
                    "authored_branches": list(row.authored_branches or []),
                    "goal": goal, "goal_source": source,
                    "initial_prompt": row.initial_prompt})
    return out


async def main(argv: "list[str] | None" = None) -> int:
    import argparse
    import os
    import sys
    from server.db import db_session, init_db
    from server.ipc.client import DaemonClient, DaemonUnavailable
    from server.ipc.paths import sessiond_endpoint
    from server.sessions.remote import RemoteOrchestrator

    parser = argparse.ArgumentParser(description="Audit archived sessions vs the completion bar.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; do not unarchive premature sessions")
    args = parser.parse_args(argv)

    db_url = os.environ.get("CCSWITCH_DB_URL")
    if not db_url:
        print(
            "error: CCSWITCH_DB_URL must be set to the daemon's database URL "
            "(the audit reads archived sessions from it).",
            file=sys.stderr,
        )
        return 2
    await init_db(db_url=db_url)

    rows = await load_archived_rows(db_session)

    async def _noop(_sid):
        return None

    if args.dry_run:
        results = await run_audit(rows, _real_gh, _noop, dry_run=True, gh_num_fn=_real_gh_num)
        print(format_report(results))
        return 0

    client = DaemonClient(sessiond_endpoint())
    try:
        await client.info()   # pre-flight: no live daemon -> DaemonUnavailable
    except DaemonUnavailable as exc:
        # Never unarchive standalone — the live daemon wouldn't see it. Report only.
        print(f"daemon unavailable ({exc}); reporting only. Start the daemon and re-run "
              f"without --dry-run to unarchive the premature sessions below.")
        results = await run_audit(rows, _real_gh, _noop, dry_run=True, gh_num_fn=_real_gh_num)
        print(format_report(results))
        return 0

    orch = RemoteOrchestrator(client)
    results = await run_audit(rows, _real_gh, lambda sid: orch.unarchive(sid), dry_run=False,
                               gh_num_fn=_real_gh_num)
    print(format_report(results))
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shell
    import sys
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
