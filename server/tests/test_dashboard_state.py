"""Plotroom DashboardState mapping — pure-function unit tests.

Field vocabulary mirrors plotroom@5166b52 frontend/src/types.ts (PrView,
StageResult, CheckView). Phase-1 fields ccswitch can't populate yet are
emitted as honest nulls/False — never fabricated.
"""
from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo
from server.pipeline.dashboard_state import check_view, stage_result


def _row(**kw) -> CheckRow:
    base = dict(name="test/unit", workflow="CI", status="COMPLETED",
                conclusion="success", duration_seconds=120, required=True,
                overdue=False, rem_low_seconds=None, rem_nominal_seconds=None,
                rem_high_seconds=None)
    base.update(kw)
    return CheckRow(**base)


def _pr(**kw) -> PullRequestInfo:
    base = dict(number=7, url="https://github.com/o/r/pull/7", state="open",
                checks="pending", title="t", stage="ci", repo="o/r")
    base.update(kw)
    return PullRequestInfo(**base)


def test_check_view_completed_success():
    v = check_view(_row())
    assert v == {
        "name": "test/unit", "status": "COMPLETED", "conclusion": "SUCCESS",
        "isRequired": True, "workflowName": "CI",
        "elapsedSeconds": 120, "expectedSeconds": 120, "url": None,
        "expectedLowSeconds": None, "expectedHighSeconds": None,
        "waitKind": None, "blockedOn": None, "waitingSeconds": None,
        "expectedRunnerWaitSeconds": None,
        "flakeRatePct": None, "likelyFlake": False,
        "regressed": False, "regression": None, "rerunInProgress": False,
    }


def test_check_view_in_progress_projects_expected_from_remaining():
    v = check_view(_row(status="IN_PROGRESS", conclusion="pending",
                        duration_seconds=60, rem_low_seconds=10,
                        rem_nominal_seconds=30, rem_high_seconds=90))
    assert v["conclusion"] is None            # not COMPLETED → no conclusion
    assert v["elapsedSeconds"] == 60
    assert v["expectedSeconds"] == 90         # elapsed + rem_nominal
    assert v["expectedLowSeconds"] == 70      # elapsed + rem_low
    assert v["expectedHighSeconds"] == 150    # elapsed + rem_high


def test_stage_result_open_ci():
    s = stage_result(_pr(eta_seconds=300, eta_percent=40, eta_overdue=False,
                         eta_low_seconds=200, eta_high_seconds=500))
    assert s == {"stage": "ci", "substate": None, "percent": 40,
                 "etaSeconds": 300, "etaRangeSeconds": [200, 500],
                 "overdue": False}


def test_stage_result_parked_substates():
    assert stage_result(_pr(stage="parked", state="draft", is_draft=True))["substate"] == "draft"
    assert stage_result(_pr(stage="parked", mergeable="CONFLICTING"))["substate"] == "conflicting"
    assert stage_result(_pr(stage="parked", checks="failing"))["substate"] == "ci-failed"
    assert stage_result(_pr(stage="parked"))["substate"] is None


def test_stage_result_queue_from_merge_queue_flag():
    ok = stage_result(_pr(in_merge_queue=True))
    assert ok["stage"] == "queue" and ok["substate"] is None
    failing_group = CheckGroup(kind="merge", checks=(_row(conclusion="failure"),),
                               total_seconds=0, cost_usd=None)
    bad = stage_result(_pr(in_merge_queue=True, check_groups=(failing_group,)))
    assert bad["substate"] == "group-failed"


def test_stage_result_merged_and_awaiting_prod():
    assert stage_result(_pr(state="merged", merged=True, deployed=True))["stage"] == "merged"
    s = stage_result(_pr(state="merged", merged=True, deployed=False,
                         deployed_envs=("qa",)))
    assert s["stage"] == "awaiting-prod"
    # merged, no deploy pipeline at all → plain merged
    assert stage_result(_pr(state="merged", merged=True))["stage"] == "merged"
