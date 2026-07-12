"""Assemble plotroom-shaped DashboardState JSON from ccswitch pipeline data.

Vocabulary contract: plotroom@5166b52 frontend/src/types.ts. Keys are
camelCase (this is a wire format, not internal state). Fields ccswitch
cannot populate yet (flake, regression, runner-wait, cost, timeline,
mergeEtaSim, workflow-impact) are emitted as honest nulls/False; later
phases fill them in — never fabricate.
"""
from __future__ import annotations

from collections.abc import Sequence

from server.github.merge_queue import QueueEntry
from server.github.pr_status import CheckGroup, CheckRow, PipelinePr, PullRequestInfo
from server.pipeline.merge_train import MergeTrainState

# Plotroom's default merge-queue batch size (config.example.json). Becomes a
# real setting in Phase 4 (spec §5); a constant until then.
DEFAULT_BATCH_SIZE = 6


def check_view(row: CheckRow) -> dict:
    elapsed = row.duration_seconds
    expected = expected_low = expected_high = None
    if row.status == "COMPLETED":
        expected = elapsed
    elif row.rem_nominal_seconds is not None:
        base = elapsed or 0
        expected = base + row.rem_nominal_seconds
        if row.rem_low_seconds is not None:
            expected_low = base + row.rem_low_seconds
        if row.rem_high_seconds is not None:
            expected_high = base + row.rem_high_seconds
    conclusion = row.conclusion.upper() if row.status == "COMPLETED" and row.conclusion else None
    return {
        "name": row.name, "status": row.status, "conclusion": conclusion,
        "isRequired": row.required, "workflowName": row.workflow,
        "elapsedSeconds": elapsed, "expectedSeconds": expected, "url": None,
        "expectedLowSeconds": expected_low, "expectedHighSeconds": expected_high,
        "waitKind": None, "blockedOn": None, "waitingSeconds": None,
        "expectedRunnerWaitSeconds": None,
        "flakeRatePct": None, "likelyFlake": False,
        "regressed": False, "regression": None, "rerunInProgress": False,
    }


def _merge_group(pr: PullRequestInfo) -> CheckGroup | None:
    for g in pr.check_groups:
        if g.kind == "merge":
            return g
    return None


def _group_failed(pr: PullRequestInfo) -> bool:
    g = _merge_group(pr)
    return bool(g and any(c.conclusion == "failure" for c in g.checks))


def stage_result(pr: PullRequestInfo) -> dict:
    if pr.merged or pr.state == "merged":
        # qa-deploy detection needs Phase-3 deploy tracking; until then a
        # merged PR on a deploy-enabled repo that hasn't reached prod is
        # awaiting-prod, and everything else is plain merged.
        stage = "awaiting-prod" if (pr.deployed_envs and not pr.deployed) else "merged"
        substate = None
    elif pr.in_merge_queue:
        stage = "queue"
        substate = "group-failed" if _group_failed(pr) else None
    else:
        stage = pr.stage or "ready"
        substate = None
        if stage == "parked":
            if pr.is_draft or pr.state == "draft":
                substate = "draft"
            elif pr.mergeable == "CONFLICTING":
                substate = "conflicting"
            elif pr.checks == "failing":
                substate = "ci-failed"
    rng = (
        [pr.eta_low_seconds, pr.eta_high_seconds]
        if pr.eta_low_seconds is not None and pr.eta_high_seconds is not None
        else None
    )
    return {"stage": stage, "substate": substate, "percent": pr.eta_percent,
            "etaSeconds": pr.eta_seconds, "etaRangeSeconds": rng,
            "overdue": pr.eta_overdue}


# QueueEntry states that mean "in a building merge group" when a batch oid exists.
_BUILDING_STATES = frozenset({"AWAITING_CHECKS", "MERGEABLE", "LOCKED", "QUEUED"})


def pr_view(p: PipelinePr, queue_ahead: int | None = None, fixable: bool = False) -> dict:
    pr = p.pr
    head = next((g for g in pr.check_groups if g.kind == "head"), None)
    merge = _merge_group(pr)
    return {
        "repo": pr.repo, "number": pr.number, "title": pr.title, "url": pr.url,
        "stage": stage_result(pr),
        "mergeStateStatus": pr.merge_state_status or None,
        "queueAheadCount": queue_ahead,
        "checks": [check_view(c) for c in head.checks] if head else [],
        "timeline": None, "touchesWorkflows": False, "workflowImpact": None,
        "groupChecks": [check_view(c) for c in merge.checks] if (merge and pr.in_merge_queue) else None,
        "mergeEtaSim": None,
        "costMinutes": None, "costDollars": None, "costDollarsPartial": False,
        # ccswitch extension (spec §6): the authoring session + Fix eligibility.
        "session": (
            {"id": p.session_id, "name": p.session_name, "fixable": fixable}
            if p.session_id else None
        ),
    }


def queue_view(entries: Sequence[QueueEntry], train: MergeTrainState | None) -> dict:
    groups: dict[str, dict] = {}
    waiting: list[dict] = []
    unmergeable: list[int] = []
    blocked: list[int] = []
    conflict_seen = False
    for e in sorted(entries, key=lambda e: (e.position is None, e.position or 0)):
        if e.state == "UNMERGEABLE":
            # GitHub marks cascade victims UNMERGEABLE too; without a DIRTY
            # snapshot, presume the front-most is the genuine conflict
            # (plotroom types.ts unmergeableCulprit contract).
            (blocked if conflict_seen else unmergeable).append(e.pr_number)
            conflict_seen = True
        elif e.batch_head_oid and e.state in _BUILDING_STATES:
            g = groups.setdefault(e.batch_head_oid, {
                "oid": e.batch_head_oid, "prNumbers": [],
                "percent": None, "etaSeconds": None, "failed": False,
            })
            g["prNumbers"].append(e.pr_number)
            if e.checks_total and e.checks_done is not None:
                g["percent"] = round(100 * e.checks_done / e.checks_total)
            if e.eta_seconds is not None:
                g["etaSeconds"] = e.eta_seconds
            if e.group_checks == "failing":
                g["failed"] = True
        else:
            waiting.append({"prNumber": e.pr_number, "position": e.position or 0,
                            "sim": None})
    return {
        "groups": list(groups.values()), "waiting": waiting,
        "unmergeable": unmergeable, "queueBlocked": blocked,
        "unmergeableCulprit": unmergeable[0] if unmergeable else None,
        "batchSize": DEFAULT_BATCH_SIZE,
        "depth": train.depth if train else len(entries),
        "trainsPerHour": train.trains_per_hour if train else None,
        "batchSuccessRatePct": train.batch_success_pct if train else None,
        "ejects24h": train.ejects_24h if train else 0,
    }


def _fixable(pr: PullRequestInfo) -> bool:
    return pr.checks == "failing" and pr.state in ("open", "draft")


def dashboard_state(
    prs_by_repo: dict[str, list[PipelinePr]],
    queues: dict[str, list[QueueEntry]],
    trains: dict[str, MergeTrainState],
    has_deploy: dict[str, bool],
    generated_at: str,
) -> dict:
    repos = []
    for repo in sorted(prs_by_repo):
        entries = queues.get(repo, [])
        pos = {e.pr_number: e.position for e in entries}
        prs = []
        for p in prs_by_repo[repo]:
            position = pos.get(p.pr.number)
            prs.append(pr_view(
                p,
                queue_ahead=(position - 1) if position is not None else None,
                fixable=_fixable(p.pr),
            ))
        repos.append({
            "repo": repo, "hasDeploy": has_deploy.get(repo, False),
            "prs": prs,
            "queue": queue_view(entries, trains.get(repo)) if entries else None,
        })
    return {"generatedAt": generated_at, "staleSince": None, "repos": repos}
