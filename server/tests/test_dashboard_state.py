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
    base: dict = dict(name="test/unit", workflow="CI", status="COMPLETED",
                conclusion="success", duration_seconds=120, required=True,
                overdue=False, rem_low_seconds=None, rem_nominal_seconds=None,
                rem_high_seconds=None)
    base.update(kw)
    return CheckRow(**base)


def _pr(**kw) -> PullRequestInfo:
    base: dict = dict(number=7, url="https://github.com/o/r/pull/7", state="open",
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
    base: dict = dict(pr_number=1, position=1, state="AWAITING_CHECKS",
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


def test_dashboard_state_injects_queue_substates():
    prs = {"o/r": [PipelinePr(pr=_pr(number=12, in_merge_queue=True)),
                   PipelinePr(pr=_pr(number=13, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=12, position=1, state="UNMERGEABLE"),
                      _entry(pr_number=13, position=2, state="UNMERGEABLE")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False}, generated_at="x")
    by_num = {p["number"]: p for p in st["repos"][0]["prs"]}
    assert by_num[12]["stage"]["substate"] == "unmergeable"
    assert by_num[13]["stage"]["substate"] == "queue-blocked"


def test_dashboard_state_group_failed_wins_over_queue_substate():
    # group-failed (from stage_result) must not be clobbered by injection
    failing_group = CheckGroup(kind="merge", checks=(_row(conclusion="failure"),),
                               total_seconds=0, cost_usd=None)
    prs = {"o/r": [PipelinePr(pr=_pr(number=12, in_merge_queue=True,
                                     check_groups=(failing_group,)))]}
    queues = {"o/r": [_entry(pr_number=12, position=1, state="UNMERGEABLE")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False}, generated_at="x")
    assert st["repos"][0]["prs"][0]["stage"]["substate"] == "group-failed"


# --- mergeEtaSim wiring (Task 3) -------------------------------------------


def _sim_ctx(**kw) -> dict:
    base: dict = dict(duration_samples=[600], eject_prob=0.0)
    base.update(kw)
    return base


def test_queue_view_sim_attaches_to_waiting_entry():
    # A lone waiting entry, no building train ahead => trains=1, base ETA 0.
    entries = [_entry(pr_number=14, position=1, state="QUEUED")]
    q = queue_view(entries, None, sim_ctx=_sim_ctx())
    assert q["waiting"] == [{
        "prNumber": 14, "position": 1,
        "sim": {"p50Secs": 600, "p90Secs": 600, "trainsAhead": 0,
                "assumesEjects": False},
    }]


def test_queue_view_sim_none_without_ctx():
    entries = [_entry(pr_number=14, position=1, state="QUEUED")]
    assert queue_view(entries, None)["waiting"][0]["sim"] is None


def test_queue_view_sim_none_without_samples():
    entries = [_entry(pr_number=14, position=1, state="QUEUED")]
    q = queue_view(entries, None, sim_ctx=_sim_ctx(duration_samples=[]))
    assert q["waiting"][0]["sim"] is None


def test_queue_view_sim_counts_queued_ahead_and_building_train_eta():
    # A building group at position 1 (its etaSeconds is the currentTrainEta),
    # a queued waiting entry at position 2, and my waiting entry at position 3.
    entries = [
        _entry(pr_number=10, position=1, batch_head_oid="abc",
               state="AWAITING_CHECKS", eta_seconds=120),
        _entry(pr_number=11, position=2, state="QUEUED"),
        _entry(pr_number=12, position=3, state="QUEUED"),
    ]
    q = queue_view(entries, None, sim_ctx=_sim_ctx())
    sims = {w["prNumber"]: w["sim"] for w in q["waiting"]}
    # PR 12: ahead = {10(building), 11(queued)}; building=1 => queuedAhead=1;
    # trains = ceil(2/6) = 1; deepest building eta 120 => base 120.
    assert sims[12]["p50Secs"] == 720          # 120 + 1 * 600
    assert sims[12]["trainsAhead"] == 1        # trains(1) - 1 + 1 (building ahead)
    # PR 11: ahead = {10(building)}; building=1 => queuedAhead=0; base 120.
    assert sims[11]["p50Secs"] == 720
    assert sims[11]["trainsAhead"] == 1


def test_queue_view_sim_excludes_mergeable_unmergeable_from_queued_ahead():
    # UNMERGEABLE + MERGEABLE ahead must not inflate queuedAhead.
    entries = [
        _entry(pr_number=9, position=1, state="UNMERGEABLE"),
        _entry(pr_number=10, position=2, state="MERGEABLE", batch_head_oid="abc"),
        _entry(pr_number=11, position=3, state="QUEUED"),
        _entry(pr_number=12, position=4, state="QUEUED"),
    ]
    q = queue_view(entries, None, sim_ctx=_sim_ctx())
    sims = {w["prNumber"]: w["sim"] for w in q["waiting"]}
    # PR 12: ahead excludes 9 (UNMERGEABLE) + 10 (MERGEABLE); only 11 counts
    # => queuedAhead=1, no building train => base 0, trains=ceil(2/6)=1.
    assert sims[12]["p50Secs"] == 600
    assert sims[12]["trainsAhead"] == 0


def test_dashboard_state_threads_sim_to_queue_and_pr_view():
    prs = {"o/r": [PipelinePr(pr=_pr(number=14, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=14, position=1, state="QUEUED")]}
    sim_ctx = {"o/r": {"duration_samples": [600], "eject_prob": 0.0}}
    st = dashboard_state(prs, queues, {}, {"o/r": False},
                         generated_at="x", sim_ctx=sim_ctx)
    repo = st["repos"][0]
    waiting_sim = repo["queue"]["waiting"][0]["sim"]
    assert waiting_sim == {"p50Secs": 600, "p90Secs": 600, "trainsAhead": 0,
                           "assumesEjects": False}
    # Same sim lands on the matching pr_view.mergeEtaSim (waiting queue PR).
    assert repo["prs"][0]["mergeEtaSim"] == waiting_sim


def test_dashboard_state_sim_absent_for_non_waiting_pr():
    # An UNMERGEABLE queue PR is not waiting => mergeEtaSim stays None even with ctx.
    prs = {"o/r": [PipelinePr(pr=_pr(number=12, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=12, position=1, state="UNMERGEABLE")]}
    sim_ctx = {"o/r": {"duration_samples": [600], "eject_prob": 0.0}}
    st = dashboard_state(prs, queues, {}, {"o/r": False},
                         generated_at="x", sim_ctx=sim_ctx)
    assert st["repos"][0]["prs"][0]["mergeEtaSim"] is None


def test_dashboard_state_backward_compatible_without_sim_ctx():
    prs = {"o/r": [PipelinePr(pr=_pr(number=14, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=14, position=1, state="QUEUED")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False}, generated_at="x")
    assert st["repos"][0]["queue"]["waiting"][0]["sim"] is None
    assert st["repos"][0]["prs"][0]["mergeEtaSim"] is None


# --------------------------------------------------------------------------- #
# Task 6 wiring: deploy block, hasDeploy override, timeline emission            #
# --------------------------------------------------------------------------- #

def test_stage_result_merged_uses_timeline_facts_for_configured_repo():
    # Configured repo (timeline dict present): qa live + prod not → awaiting-prod.
    tl = {"qaLiveAt": "2026-07-12T01:00:00Z", "prodLiveAt": None}
    assert stage_result(_pr(state="merged", merged=True), tl)["stage"] == "awaiting-prod"
    # prod live → plain merged even though qa live too.
    tl2 = {"qaLiveAt": "2026-07-12T01:00:00Z", "prodLiveAt": "2026-07-12T02:00:00Z"}
    assert stage_result(_pr(state="merged", merged=True), tl2)["stage"] == "merged"
    # neither live → plain merged (deploy block carries the truth).
    tl3 = {"qaLiveAt": None, "prodLiveAt": None}
    assert stage_result(_pr(state="merged", merged=True), tl3)["stage"] == "merged"
    # timeline facts win over the deployed_envs fallback: env-live but prod-facts
    # say merged → merged (not awaiting-prod).
    assert stage_result(
        _pr(state="merged", merged=True, deployed_envs=("qa",)), tl2
    )["stage"] == "merged"


def _deploy_block(**kw) -> dict:
    base = {"envs": [{"name": "qa", "liveSha": "abc", "reachable": True}],
            "awaitingQa": 1, "awaitingProd": 0,
            "chain": {"entries": [], "inFlight": None, "supersededCount": 0}}
    base.update(kw)
    return base


def test_dashboard_state_attaches_deploy_block_and_overrides_has_deploy():
    prs = {"o/r": [PipelinePr(pr=_pr(number=7, state="open"))]}
    block = _deploy_block()
    # has_deploy says False (stage-model derivation), but deploy_ctx presence wins.
    st = dashboard_state(prs, {}, {}, {"o/r": False}, generated_at="x",
                         deploy_ctx={"o/r": block})
    repo = st["repos"][0]
    assert repo["hasDeploy"] is True
    assert repo["deploy"] == block


def test_dashboard_state_no_deploy_block_keeps_derivation_and_omits_key():
    prs = {"o/r": [PipelinePr(pr=_pr(number=7, state="open"))]}
    # No deploy_ctx entry → keep the stage-model hasDeploy + no deploy key.
    st = dashboard_state(prs, {}, {}, {"o/r": True}, generated_at="x", deploy_ctx={})
    repo = st["repos"][0]
    assert repo["hasDeploy"] is True
    assert "deploy" not in repo


def test_pr_view_workflow_impact_gated_on_touches_workflows():
    touching = PipelinePr(pr=_pr(touches_workflows=True))
    impact = {"summary": ["+ lint joins the merge_group gate"]}
    v = pr_view(touching, workflow_impact=impact)
    assert v["touchesWorkflows"] is True
    assert v["workflowImpact"] == impact
    # Touching PR without a computed card → honest null.
    assert pr_view(touching)["workflowImpact"] is None
    # Non-touching PR never carries a card, even if one is passed.
    plain = PipelinePr(pr=_pr())
    v2 = pr_view(plain, workflow_impact=impact)
    assert v2["touchesWorkflows"] is False
    assert v2["workflowImpact"] is None


def test_dashboard_state_threads_workflow_impact_ctx():
    impact = {"summary": ["required-check set grows by 1: 3 → 4 checks"]}
    prs = {"o/r": [
        PipelinePr(pr=_pr(number=7, touches_workflows=True)),
        PipelinePr(pr=_pr(number=8)),
    ]}
    st = dashboard_state(
        prs, {}, {}, {"o/r": False}, generated_at="x",
        workflow_impact_ctx={"o/r": {7: impact, 8: impact}},
    )
    by_num = {p["number"]: p for p in st["repos"][0]["prs"]}
    assert by_num[7]["workflowImpact"] == impact
    assert by_num[8]["workflowImpact"] is None   # not touching → gated off


def test_dashboard_state_emits_timeline_for_merged_only():
    merged_tl = {"createdAt": None, "firstGreenAt": None, "enqueuedAt": None,
                 "mergedAt": "2026-07-12T00:00:00Z", "qaLiveAt": "2026-07-12T01:00:00Z",
                 "prodLiveAt": None}
    open_tl = {"mergedAt": None, "qaLiveAt": None, "prodLiveAt": None}
    prs = {"o/r": [
        PipelinePr(pr=_pr(number=7, state="merged", merged=True)),
        PipelinePr(pr=_pr(number=8, state="open")),
    ]}
    st = dashboard_state(
        prs, {}, {}, {"o/r": False}, generated_at="x",
        timeline_ctx={"o/r": {7: merged_tl, 8: open_tl}},
    )
    by_num = {p["number"]: p for p in st["repos"][0]["prs"]}
    # Merged PR carries the timeline verbatim + awaiting-prod from its qa fact.
    assert by_num[7]["timeline"] == merged_tl
    assert by_num[7]["stage"]["stage"] == "awaiting-prod"
    # Open PR stays timeline None even though a dict was passed for it.
    assert by_num[8]["timeline"] is None
