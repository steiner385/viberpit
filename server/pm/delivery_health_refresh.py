"""Production delivery health refresh (#3404): gather one product's facts, store the
readout on its ladder, and open, update or clear the owner ask.

Runs right after the product's milestone refresh (``SessionOrchestrator.
_milestones_refresh_run``), so it shares the Milestones Refresh mode and its
10-minute cadence. A product takes part when it has a ladder AND its primary repo
has an enabled prod deploy environment (``RepoDeployConfig`` — the deploy watcher's
own source of prod's health URL); the gate workflows come from its policy
(``delivery_verified_workflows``). The rule itself is ``server/pm/delivery_health.py``.

Reads: prod's health endpoint (``deploy_watch.fetch_live_sha``), one compare
(``deploy_watch.compare``), one run list per gate workflow, one GraphQL search for
the open ``ci:regressed`` issues and their closing PRs, and the live sessions'
origin refs. Writes: the ``@delivery`` measurement row, the episode's ask
(``MilestoneNote``), and at most one ``decision_needed`` push per prod SHA, never in
quiet hours (deferred to a later tick, not dropped).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from server.audit import record_audit
from server.automations.product_policy import effective_policy
from server.db import db_session
from server.db.models import (MilestoneLadder, MilestoneMeasurement, MilestoneNote, Project,
                              RepoDeployConfig, Session as DbSession)
from server.github.deploy_envs import classify_env
from server.github.host_failures import log_gh_failure
from server.pipeline import deploy_watch
from server.pm import delivery_health as dh, product_repos
from server.pm.desired_state import LIVE_SESSION_STATUSES
from server.pm.map_changes import resolve_default_branch
from server.pm.milestone_asks import ASK_KIND, OPEN_ASK_STATUS
from server.pm.milestone_github import streak_from_runs
from server.time_iso import aware_utc, parse_iso_utc

log = logging.getLogger(__name__)

#: Runs read per gate workflow: enough to find the newest green one on a red streak.
RUNS_LIMIT = 30
#: Regressed issues read (the ask names them all; more than this is its own problem).
REGRESSED_LIMIT = 20
#: Audit rows for the ask's lifecycle.
AUDIT_TYPE = "pm.delivery_health.ask"

GraphQL = Callable[[str, str], Awaitable[dict]]
ListRuns = Callable[..., list[dict]]
Notify = Callable[..., Awaitable[None]]
QuietHours = Callable[[datetime], Awaitable[bool]]


@dataclass(frozen=True)
class Target:
    project_id: str
    slug: str
    ladder_id: str
    repo: str
    health_url: str
    sha_key: str
    gate_workflows: tuple[str, ...]
    #: The episode a push already went out for (the stored readout's ``notified``).
    notified: str | None
    live_refs: frozenset[str]


@dataclass(frozen=True)
class Outcome:
    facts: dh.DeliveryFacts
    verdict: dh.Verdict
    #: ``opened`` / ``updated`` / ``cleared`` / ``None`` (nothing to do).
    ask: str | None
    notified: bool


def prod_env(environments: Any) -> dict | None:
    """The first prod environment (``classify_env``) of a ``RepoDeployConfig`` row."""
    try:
        envs = json.loads(environments or "[]")
    except (ValueError, TypeError):
        return None
    for env in envs if isinstance(envs, list) else ():
        if (isinstance(env, dict) and isinstance(env.get("name"), str)
                and isinstance(env.get("healthUrl"), str)
                and classify_env(env["name"]) == "prod"):
            return env
    return None


async def load_target(project_id: str) -> Target | None:
    """What one product's delivery refresh needs, or ``None`` when it does not take
    part (no ladder, no primary repo, or no enabled prod deploy environment)."""
    async with db_session() as db:
        ladder = (await db.execute(select(MilestoneLadder).where(
            MilestoneLadder.project_id == project_id))).scalar_one_or_none()
        project = await db.get(Project, project_id)
        if ladder is None or project is None:
            return None
        repo = await product_repos.primary_repo(db, project_id)
        if not repo:
            return None
        cfg = await db.get(RepoDeployConfig, repo)
        env = prod_env(cfg.environments) if cfg is not None and cfg.enabled else None
        if env is None:
            return None
        row = (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.ladder_id == ladder.id, MilestoneMeasurement.scope == dh.SCOPE,
            MilestoneMeasurement.key == dh.KEY))).scalar_one_or_none()
        live = (await db.execute(select(DbSession.origin_ref).where(
            DbSession.status.in_(LIVE_SESSION_STATUSES)))).scalars().all()
    policy = effective_policy(project.automation_policy)
    workflows = tuple(str(w).strip() for w in policy.get("delivery_verified_workflows") or ()
                      if str(w).strip())
    notified = (row.value or {}).get("notified") if row is not None else None
    return Target(project_id=project_id, slug=project.slug or project_id, ladder_id=ladder.id,
                  repo=repo, health_url=env["healthUrl"],
                  sha_key=env.get("shaKey") or "commitSha", gate_workflows=workflows,
                  notified=notified if isinstance(notified, str) else None,
                  live_refs=frozenset(r for r in live if r))


# ---------------------------------------------------------------- GitHub reads


def regressed_query(repo: str) -> str:
    search = json.dumps(f'repo:{repo} is:issue is:open label:"{dh.REGRESSED_LABEL}"')
    return ("query { search(type: ISSUE, first: %d, query: %s) { nodes { ... on Issue { "
            "number title url repository { nameWithOwner } "
            "closedByPullRequestsReferences(first: 5, includeClosedPrs: false) { nodes { "
            "number state repository { nameWithOwner } } } } } } }"
            % (REGRESSED_LIMIT, search))


def parse_regressed(payload: Any, live_refs: frozenset[str]) -> tuple[dh.RegressedIssue, ...]:
    """The regressed issues of a ``regressed_query`` payload. Raises ``ValueError`` on
    a payload with no ``search`` — a failed read must not read as "none open"."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    search = data.get("search") if isinstance(data, Mapping) else None
    if not isinstance(search, Mapping):
        raise ValueError("regressed-issue search returned no data")
    out: list[dh.RegressedIssue] = []
    for node in search.get("nodes") or ():
        if not isinstance(node, Mapping) or not node.get("number"):
            continue
        repo = (node.get("repository") or {}).get("nameWithOwner") or ""
        ref = f"{repo}#{node['number']}"
        prs = tuple(
            f"{(p.get('repository') or {}).get('nameWithOwner') or repo}#{p.get('number')}"
            for p in ((node.get("closedByPullRequestsReferences") or {}).get("nodes") or ())
            if isinstance(p, Mapping) and p.get("number") and p.get("state") == "OPEN")
        out.append(dh.RegressedIssue(ref=ref, title=str(node.get("title") or ""),
                                     url=str(node.get("url") or ""),
                                     session=ref in live_refs, prs=prs))
    return tuple(sorted(out, key=lambda r: r.ref))


async def gather(target: Target, *, graphql: GraphQL, list_runs: ListRuns,
                 http_get: deploy_watch.HttpGet, gh_run: deploy_watch.GhRun,
                 default_branch: Callable[[str], Awaitable[str]]) -> dh.DeliveryFacts:
    """Every fact the rule reads. Never raises: a failed read is ``None`` on its fact
    plus a line in ``errors``."""
    repo = target.repo
    errors: list[str] = []
    prod_sha, _reachable = await deploy_watch.fetch_live_sha(
        http_get, target.health_url, target.sha_key)
    if prod_sha is None:
        errors.append("prod health endpoint unreachable")
    branch = await default_branch(repo)

    behind: int | None = None
    oldest: datetime | None = None
    if prod_sha:
        body = await deploy_watch.compare(gh_run, repo, prod_sha, branch, query="?per_page=1")
        ahead = body.get("ahead_by") if body is not None else None
        if body is not None and isinstance(ahead, int):
            behind = ahead
            commits = body.get("commits") or []
            first = commits[0] if commits and isinstance(commits[0], Mapping) else {}
            oldest = parse_iso_utc(((first.get("commit") or {}).get("committer") or {})
                                   .get("date"))
            if behind and oldest is None:
                errors.append("compare listed no commit dates")
        else:
            errors.append(f"compare {dh.short(prod_sha)}...{branch} failed")

    verified: dict[str, str] = {}
    streak: int | None = None
    streak_url: str | None = None
    for i, wf in enumerate(target.gate_workflows):
        try:
            runs = await asyncio.to_thread(list_runs, repo, wf, branch=branch, event=None,
                                           limit=RUNS_LIMIT)
        except Exception as exc:
            log_gh_failure(log, str(exc), "delivery_health.runs_failed repo=%s workflow=%s",
                           repo, wf, demote_edge_failures=True)
            errors.append(f"{wf} runs not read")
            continue
        for r in runs:  # newest first: the first green run per SHA wins
            sha = r.get("head_sha")
            if (r.get("status") == "completed" and r.get("conclusion") == "success"
                    and isinstance(sha, str) and sha not in verified):
                verified[sha] = str(r.get("html_url") or "")
        if i == 0:
            streak, _at, streak_url = streak_from_runs(
                [r for r in runs if r.get("event") == "schedule"])

    regressed: tuple[dh.RegressedIssue, ...] | None
    try:
        payload = await graphql(repo.partition("/")[0], regressed_query(repo))
        regressed = parse_regressed(payload, target.live_refs)
    except Exception as exc:
        log_gh_failure(log, str(exc), "delivery_health.regressed_failed repo=%s", repo,
                       demote_edge_failures=True)
        errors.append(f"{dh.REGRESSED_LABEL} issues not read")
        regressed = None

    return dh.DeliveryFacts(
        repo=repo, branch=branch, prod_sha=prod_sha, behind_commits=behind,
        oldest_unshipped_at=oldest, verified=verified, gate_workflows=target.gate_workflows,
        streak=streak if target.gate_workflows else None, streak_url=streak_url,
        regressed=regressed, errors=tuple(errors))


# ---------------------------------------------------------------- writes


async def _sync_ask(db, target: Target, facts: dh.DeliveryFacts, verdict: dh.Verdict,
                    now: datetime) -> str | None:
    """Open, update or clear this signal's ask. One ask per episode (prod SHA): an ask
    a PERSON closed stays closed until the next promotion; one this signal cleared
    reopens if the condition returns on the same SHA."""
    asks = (await db.execute(select(MilestoneNote).where(
        MilestoneNote.ladder_id == target.ladder_id, MilestoneNote.kind == ASK_KIND,
        MilestoneNote.created_by == dh.ASK_AUTHOR))).scalars().all()
    open_asks = [n for n in asks if n.status == OPEN_ASK_STATUS]
    ref = dh.episode_ref(facts)
    if not verdict.escalate:
        for n in open_asks:
            n.status, n.resolved_by, n.resolved_at, n.updated_at = (
                "done", dh.ASK_AUTHOR, now, now)
        if open_asks:
            await record_audit(db, AUDIT_TYPE, dh.ASK_AUTHOR, {
                "slug": target.slug, "action": "cleared", "refs": [n.ref for n in open_asks],
                "prodSha": facts.prod_sha, "state": verdict.state}, via="automation")
            return "cleared"
        return None
    text, url = dh.ask_text(facts, verdict), dh.ask_url(facts, verdict)
    # A previous episode's ask is over: prod moved.
    for n in open_asks:
        if n.ref != ref:
            n.status, n.resolved_by, n.resolved_at, n.updated_at = (
                "done", dh.ASK_AUTHOR, now, now)
    mine = next((n for n in asks if n.ref == ref), None)
    if mine is not None and mine.status != OPEN_ASK_STATUS and mine.resolved_by != dh.ASK_AUTHOR:
        return None   # a person closed this episode's ask: respect it
    action = "updated"
    if mine is None:
        mine = MilestoneNote(ladder_id=target.ladder_id, milestone_key=None, kind=ASK_KIND,
                             ref=ref, created_by=dh.ASK_AUTHOR, created_at=now)
        db.add(mine)
        action = "opened"
    elif mine.status != OPEN_ASK_STATUS:
        action = "opened"
    if action == "updated" and mine.text == text and mine.url == url:
        return None
    mine.text, mine.url, mine.status, mine.updated_at = text, url, OPEN_ASK_STATUS, now
    mine.resolved_by, mine.resolved_at = None, None
    await db.flush()
    if action == "opened":
        await record_audit(db, AUDIT_TYPE, dh.ASK_AUTHOR, {
            "slug": target.slug, "action": action, "ref": ref, "prodSha": facts.prod_sha,
            "reasons": list(verdict.reasons), "blockers": [b.ref for b in verdict.blockers]},
            via="automation")
    return action


async def _persist(target: Target, facts: dh.DeliveryFacts, verdict: dh.Verdict,
                   now: datetime, *, notified: str | None) -> str | None:
    async with db_session() as db:
        row = (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.ladder_id == target.ladder_id,
            MilestoneMeasurement.scope == dh.SCOPE, MilestoneMeasurement.key == dh.KEY
        ))).scalar_one_or_none()
        if row is None:
            row = MilestoneMeasurement(ladder_id=target.ladder_id, scope=dh.SCOPE, key=dh.KEY)
            db.add(row)
        row.status = verdict.state
        row.value = dh.to_wire(facts, verdict, notified=notified)
        row.measured_at = now
        row.fetched_at = now
        row.error = "; ".join(facts.errors) or None
        row.source_url = facts.compare_url
        await db.flush()
        return await _sync_ask(db, target, facts, verdict, now)


async def refresh_delivery(project_id: str, *, graphql: GraphQL, list_runs: ListRuns,
                           now: datetime, dry_run: bool = False,
                           notify: Notify | None = None, in_quiet_hours: QuietHours | None = None,
                           http_get: deploy_watch.HttpGet | None = None,
                           gh_run: deploy_watch.GhRun | None = None,
                           default_branch: Callable[[str], Awaitable[str]] = resolve_default_branch,
                           ) -> Outcome | None:
    """One product's delivery-health refresh (module docstring); ``None`` when it does
    not take part. ``dry_run`` computes and logs, writing and sending nothing.
    ``notify(title, body, *, slug, url)`` sends the escalation push."""
    at = aware_utc(now)
    assert at is not None
    target = await load_target(project_id)
    if target is None:
        return None
    facts = await gather(target, graphql=graphql, list_runs=list_runs,
                         http_get=http_get or deploy_watch._default_http_get,
                         gh_run=gh_run or deploy_watch._default_gh_run,
                         default_branch=default_branch)
    verdict = dh.evaluate(facts, at)
    if dry_run:
        log.info("delivery_health.dry_run slug=%s state=%s escalate=%s reasons=%s", target.slug,
                 verdict.state, verdict.escalate, ",".join(verdict.reasons))
        return Outcome(facts, verdict, None, False)
    episode = dh.episode_ref(facts) if verdict.escalate else None
    notified = target.notified
    ask = await _persist(target, facts, verdict, at, notified=notified)
    sent = False
    if (episode is not None and episode != notified and notify is not None
            and not (in_quiet_hours is not None and await in_quiet_hours(at))):
        if await _ask_is_open(target, episode):
            try:
                await notify(f"{target.slug}: production delivery needs you",
                             dh.ask_text(facts, verdict), slug=target.slug,
                             url=milestones_path(target.slug))
                sent = True
            except Exception:
                log.info("delivery_health.notify_failed slug=%s", target.slug, exc_info=True)
        if sent:
            await _persist(target, facts, verdict, at, notified=episode)
    return Outcome(facts, verdict, ask, sent)


async def _ask_is_open(target: Target, ref: str) -> bool:
    """Whether this episode's ask is open (a person may have closed it)."""
    async with db_session() as db:
        status = (await db.execute(select(MilestoneNote.status).where(
            MilestoneNote.ladder_id == target.ladder_id, MilestoneNote.ref == ref,
            MilestoneNote.created_by == dh.ASK_AUTHOR))).scalars().first()
    return status == OPEN_ASK_STATUS


def milestones_path(slug: str) -> str:
    """Where the push opens: the product's Milestones page (``roadmapNavState.milestonesPath``)."""
    return f"/roadmap/{slug}/milestones"
