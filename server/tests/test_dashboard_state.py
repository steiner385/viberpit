"""Plotroom DashboardState mapping — pure-function unit tests.

Field vocabulary mirrors plotroom@5166b52 frontend/src/types.ts (PrView,
StageResult, CheckView). Phase-1 fields ccswitch can't populate yet are
emitted as honest nulls/False — never fabricated.
"""
from server.github.merge_queue import QueueEntry
from server.github.pr_status import CheckGroup, CheckRow, PipelinePr, PullRequestInfo
from server.pipeline.dashboard_state import check_view, dashboard_state, pr_view, queue_view, stage_result
from server.pipeline.merge_train import MergeTrainState, TrainBuild


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


def _entry(**kw) -> QueueEntry:
    base = dict(pr_number=1, position=1, state="AWAITING_CHECKS",
                enqueued_at=None, batch_head_oid=None, group_checks=None,
                checks_total=None, checks_done=None, eta_seconds=None)
    base.update(kw)
    return QueueEntry(**base)


def test_pr_view_shape_and_session_extension():
    p = PipelinePr(pr=_pr(checks="failing", state="open"), session_id="s1",
                   session_name="fix-auth")
    v = pr_view(p, queue_ahead=None, fixable=True)
    assert v["repo"] == "o/r" and v["number"] == 7
    assert v["stage"]["stage"] == "ci"
    assert v["session"] == {"id": "s1", "name": "fix-auth", "fixable": True}
    assert v["mergeEtaSim"] is None and v["timeline"] is None
    assert v["groupChecks"] is None and v["queueAheadCount"] is None
    p2 = PipelinePr(pr=_pr())
    assert pr_view(p2)["session"] is None


def test_queue_view_groups_waiting_and_cascade():
    entries = [
        _entry(pr_number=10, position=1, batch_head_oid="abc",
               checks_total=4, checks_done=2, eta_seconds=120, group_checks="pending"),
        _entry(pr_number=11, position=2, batch_head_oid="abc"),
        _entry(pr_number=12, position=3, state="UNMERGEABLE"),
        _entry(pr_number=13, position=4, state="UNMERGEABLE"),
        _entry(pr_number=14, position=5, state="QUEUED"),
    ]
    q = queue_view(entries, None)
    assert q["groups"] == [{"oid": "abc", "prNumbers": [10, 11], "percent": 50,
                            "etaSeconds": 120, "failed": False}]
    assert q["waiting"] == [{"prNumber": 14, "position": 5, "sim": None}]
    assert q["unmergeable"] == [12]          # lowest-position genuine conflict
    assert q["queueBlocked"] == [13]         # cascade victim behind it
    assert q["unmergeableCulprit"] == 12
    assert q["batchSize"] == 6 and q["depth"] == 5


def test_queue_view_second_conflict_after_gap_is_still_blocked():
    entries = [
        _entry(pr_number=12, position=1, state="UNMERGEABLE"),
        _entry(pr_number=13, position=2),  # AWAITING_CHECKS gap (no oid → waiting)
        _entry(pr_number=14, position=3, state="UNMERGEABLE"),
    ]
    q = queue_view(entries, None)
    assert q["unmergeable"] == [12]
    assert q["queueBlocked"] == [14]
    assert q["unmergeableCulprit"] == 12


def test_queue_view_failed_group_and_train_ops():
    entries = [_entry(pr_number=10, position=1, batch_head_oid="abc",
                      group_checks="failing")]
    train = MergeTrainState(repo="o/r", depth=1, trains_per_hour=2.5,
                            batch_success_pct=80.0, ejects_24h=3,
                            oldest_wait_seconds=600, failing=(10,),
                            building=TrainBuild(10, 50, 60), blocked=(),
                            awaiting_qa=0, awaiting_prod=0, merges_next=None)
    q = queue_view(entries, train)
    assert q["groups"][0]["failed"] is True
    assert q["trainsPerHour"] == 2.5 and q["batchSuccessRatePct"] == 80.0
    assert q["ejects24h"] == 3


def test_dashboard_state_assembly_sets_queue_ahead():
    prs = {"o/r": [PipelinePr(pr=_pr(number=14, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=14, position=3, state="QUEUED")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False},
                         generated_at="2026-07-12T00:00:00Z")
    repo = st["repos"][0]
    assert st["generatedAt"] == "2026-07-12T00:00:00Z" and st["staleSince"] is None
    assert repo["repo"] == "o/r" and repo["hasDeploy"] is False
    assert repo["prs"][0]["queueAheadCount"] == 2      # position - 1
    assert repo["queue"]["depth"] == 1
    # repo with no queue entries → queue is null (QueueTrain renders nothing)
    st2 = dashboard_state(prs, {}, {}, {"o/r": False}, generated_at="x")
    assert st2["repos"][0]["queue"] is None
