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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField
from .completion_bar import BarVerdict, PrFact, PrFacts, _is_code_goal, met_bar

if TYPE_CHECKING:
    from datetime import datetime

log = logging.getLogger(__name__)


class ArchiveAudit:
    id = "archive-audit"
    name = "Archive Audit"
    description = ("Re-audits recently archived sessions against the completion bar and "
                   "auto-unarchives ones archived prematurely (no merged/armed PR).")
    modes: tuple[str, ...] = ("off", "act")
    config_schema = [
        ConfigField(key="window_days", label="Recency window (days)", type="int", default=7,
                    help="Re-audit sessions archived within this many days."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "archive_audit_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        unarchived = counts.get("unarchived", 0)
        total = counts.get("total", 0)
        if unarchived:
            level = "active"
            text = f"unarchived {unarchived} of {total} recent sessions"
        else:
            level = "idle"
            text = f"audited {total} recent sessions, none premature"
        return AutomationStatus(text=text, level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: "str | None",
                              config: dict, ctx: Any) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...


class ArchiveAuditLoop:
    """Restart-safe, non-reentrant periodic re-audit loop. Every I/O dependency is
    injected. Per tick: loads recently-archived rows, classifies them via run_audit
    (dry_run=True, offloaded to a thread so gh subprocess calls never block the loop),
    then async-unarchives the premature ones and emits a summary."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_rows: Callable[["datetime"], Awaitable[list[dict]]],
                 gh_fn: Callable[[str, str], list[dict]],
                 gh_num_fn: "Callable[[str, int], dict | None] | None",
                 unarchive_fn: Callable[[str], Awaitable[None]],
                 clock: Callable[[], float]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_rows = list_rows
        self._gh_fn = gh_fn
        self._gh_num_fn = gh_num_fn
        self._unarchive_fn = unarchive_fn
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # a tick must never crash the loop
                log.warning("archive_audit.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        window_days = int(config.get("window_days", 7))
        from datetime import datetime, timedelta
        since = datetime.utcfromtimestamp(self._clock()) - timedelta(days=window_days)
        rows = await self._list_rows(since)
        if not rows:
            return
        # The gh fetchers are BLOCKING subprocess calls and run_audit calls them
        # inline. Offload the whole classify pass to a thread so the daemon event
        # loop is never blocked. dry_run=True => run_audit does NOT unarchive inside
        # the thread; we do the unarchives on THIS loop (async orch.unarchive).
        gh_fn = self._gh_fn
        gh_num_fn = self._gh_num_fn

        def _classify():
            return asyncio.run(run_audit(
                rows, gh_fn, lambda _sid: None,
                dry_run=True, gh_num_fn=gh_num_fn))

        results = await asyncio.to_thread(_classify)
        premature = [r for r in results if r["bucket"] == "premature"]
        unarchived = []
        for r in premature:
            try:
                await self._unarchive_fn(r["id"])
                unarchived.append(r["id"])
            except Exception:
                log.warning("archive_audit loop unarchive failed sid=%s", r["id"], exc_info=True)
        from collections import Counter
        counts = Counter(r["bucket"] for r in results)
        # Publish counts on the ctx so ArchiveAudit.status() reflects the last run
        # (mirrors CicdDoctorLoop). status() reads 'total' and 'unarchived'.
        setattr(self._ctx, "archive_audit_counts", {
            "total": len(rows), "unarchived": len(unarchived),
            **{b: counts.get(b, 0) for b in ("legit", "premature", "review", "ambiguous")},
        })
        await self._ctx.emit(
            "info",
            f"re-audit: {len(rows)} recent archives — unarchived {len(unarchived)} premature",
            kind="audit",
            detail={"counts": dict(counts), "unarchived": unarchived})

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
    """Map one GraphQL PR node to a PrFact. GraphQL states are UPPERCASE
    (OPEN/MERGED/CLOSED); autoMergeRequest is non-null when auto-merge is armed;
    mergeQueueEntry is non-null when the PR is enrolled in the merge queue.

    A queued PR (mergeQueueEntry != null) is treated as armed — in_merge_queue
    flows into PrFact.is_armed so the retro audit matches the live autopilot gate.

    The check rollup (commit.statusCheckRollup.state) flows into PrFact.checks via the
    same _classify_checks SoT the live poller uses, so the audit gates an armed-but-red
    PR identically to the live path (issue #635).
    """
    from server.github.pr_status import _classify_checks
    state = str(d.get("state", "")).lower()
    is_draft = bool(d.get("isDraft", False))
    if state == "open" and is_draft:
        state = "draft"
    nodes = ((d.get("commits") or {}).get("nodes")) or []
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup") if nodes else None
    return PrFact(number=int(d.get("number", 0)), state=state, is_draft=is_draft,
                  auto_merge_enabled=d.get("autoMergeRequest") is not None,
                  in_merge_queue=d.get("mergeQueueEntry") is not None,
                  url=str(d.get("url", "")),
                  checks=_classify_checks(rollup.get("state") if rollup else None))


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


def classify(goal: str, goal_source: str, facts: PrFacts,
             pr_resolution: str | None = None, *,
             instructions: str | None = None) -> tuple[str, BarVerdict]:
    """Bucket via met_bar: met→legit; not-met+open-unarmed→premature;
    not-met+closed-target+code-goal→review; not-met+low→ambiguous."""
    v = met_bar(goal, goal_source, facts, pr_resolution, instructions=instructions)
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
        bucket, verdict = classify(row.get("goal", ""), row.get("goal_source", ""), facts,
                                    row.get("pr_resolution"),
                                    instructions=row.get("initial_prompt"))
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
    """GraphQL query for PRs by head branch, all states (OPEN/CLOSED/MERGED).
    Returns [] on ANY failure (missing gh / auth / network / non-zero / JSON error /
    GraphQL errors) — the caller then buckets the session ambiguous, never a false
    'premature'."""
    owner, _, name = repo.partition("/")
    query = (
        "query($o:String!,$n:String!,$br:String!){"
        "repository(owner:$o,name:$n){pullRequests(headRefName:$br,first:20,"
        "states:[OPEN,CLOSED,MERGED]){nodes{"
        "number url state isDraft autoMergeRequest{enabledAt} mergeQueueEntry{state} "
        "commits(last:1){nodes{commit{statusCheckRollup{state}}}}}}}}"
    )
    try:
        r = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={query}",
             "-f", f"o={owner}",
             "-f", f"n={name}",
             "-f", f"br={branch}"],
            capture_output=True, text=True, timeout=30, check=True)
    except Exception as exc:  # gh missing / auth / network / non-zero
        log.warning("archive_audit gh graphql pr list failed repo=%s head=%s: %s", repo, branch, exc)
        return []
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    if data.get("errors"):
        log.warning("archive_audit gh graphql errors repo=%s head=%s: %s", repo, branch, data["errors"])
        return []
    try:
        return data["data"]["repository"]["pullRequests"]["nodes"] or []
    except (KeyError, TypeError):
        return []


def _real_gh_num(repo: str, number: int) -> "dict | None":
    """GraphQL query for a single PR by number. Returns None on ANY failure
    (missing gh / auth / network / non-zero / JSON error / GraphQL errors /
    PR not found) — never raises."""
    owner, _, name = repo.partition("/")
    query = (
        "query($o:String!,$n:String!,$num:Int!){"
        "repository(owner:$o,name:$n){pullRequest(number:$num){"
        "number url state isDraft autoMergeRequest{enabledAt} mergeQueueEntry{state} "
        "commits(last:1){nodes{commit{statusCheckRollup{state}}}}}}}"
    )
    try:
        r = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={query}",
             "-f", f"o={owner}",
             "-f", f"n={name}",
             "-F", f"num={number}"],
            capture_output=True, text=True, timeout=30, check=True)
    except Exception as exc:
        log.warning("archive_audit gh graphql pr view failed repo=%s number=%s: %s", repo, number, exc)
        return None
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if data.get("errors"):
        log.warning("archive_audit gh graphql errors repo=%s number=%s: %s", repo, number, data["errors"])
        return None
    try:
        return data["data"]["repository"]["pullRequest"]  # None if PR not found
    except (KeyError, TypeError):
        return None


def _archived_row_to_dict(row) -> dict:
    """Map one archived Session row to the audit's row dict. Single source of truth
    for the shape shared by load_archived_rows and list_recent_archived_rows."""
    from server.sessions.goals import effective_goal
    goal, source = effective_goal(row)
    return {"id": row.id, "repo": row.repo, "branch": row.branch,
            "authored_branches": list(row.authored_branches or []),
            "goal": goal, "goal_source": source,
            "initial_prompt": row.initial_prompt,
            "pr_resolution": getattr(row, "pr_resolution", None)}


async def load_archived_rows(session_factory) -> list[dict]:
    """Load archived Session rows with the fields the audit needs (the list_archived
    seam omits repo/branch/authored_branches/goal, so read the model directly).
    session_factory() is an async context manager yielding a DB session."""
    from sqlalchemy import select as sa_select
    from server.db.models import Session as DbSession
    async with session_factory() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(DbSession.status == "archived"))).scalars().all()
    return [_archived_row_to_dict(row) for row in rows]


async def list_recent_archived_rows(session_factory, since: "datetime") -> list[dict]:
    """Archived rows with archived_at >= since (the re-audit window). Same shape as
    load_archived_rows. Rows with NULL archived_at (archived before the column existed,
    or never timestamped) are excluded — they are not 'recent'."""
    from sqlalchemy import select as sa_select
    from server.db.models import Session as DbSession
    async with session_factory() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(
                DbSession.status == "archived",
                DbSession.archived_at.isnot(None),
                DbSession.archived_at >= since,
            ))).scalars().all()
    return [_archived_row_to_dict(row) for row in rows]


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
