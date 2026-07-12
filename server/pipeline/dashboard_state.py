"""Assemble plotroom-shaped DashboardState JSON from ccswitch pipeline data.

Vocabulary contract: plotroom@5166b52 frontend/src/types.ts. Keys are
camelCase (this is a wire format, not internal state). Fields ccswitch
cannot populate yet (flake, regression, runner-wait, cost, timeline,
mergeEtaSim, workflow-impact) are emitted as honest nulls/False; later
phases fill them in — never fabricate.
"""
from __future__ import annotations

from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo

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
