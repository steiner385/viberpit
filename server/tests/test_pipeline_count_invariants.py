"""Pipeline board count invariants (follow-up to #3549).

The Pipeline page shows one number per status tile, a merge-queue lane, a deploy
backlog line and a header lane summary. Each count must read the same facts as
the element it summarises. These properties pin that on the server frame:

- every open PR the queue lane draws has stage ``queue``, and every ``queue``
  stage PR is drawn in the lane exactly once (tile == lane);
- a merged PR's deploy stage is counted by the backlog it feeds (Deploying
  tile == deploy block / deployBacklog), supersession included;
- a parked PR with required CI red reads ``ci-failed`` whatever else is wrong
  with it, so it reaches the Failed tile and the header's red count.

The tile bucketing itself (each PR in exactly one tile) is the web side's
property, in ``web/src/pipeline/__tests__/countInvariants.test.ts``.
"""
from collections import Counter
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from server.github.merge_queue import QueueEntry
from server.github.pr_status import PipelinePr, PullRequestInfo
from server.pipeline.dashboard_state import dashboard_state, stage_result
from server.pipeline.deploy_status import compute_repo_deploy

REPO = "o/r"


def _pr(**kw) -> PullRequestInfo:
    base: dict = dict(number=1, url="u", state="open", checks="passing",
                      title="t", stage="ready", repo=REPO)
    base.update(kw)
    return PullRequestInfo(**base)


def _lane_numbers(queue: dict | None) -> list[int]:
    """Every PR number the QueueTrain lane draws (groups, waiting, ✗ cars)."""
    if queue is None:
        return []
    return ([n for g in queue["groups"] for n in g["prNumbers"]]
            + [w["prNumber"] for w in queue["waiting"]]
            + queue["unmergeable"] + queue["queueBlocked"])


_ENTRY_STATES = ["QUEUED", "AWAITING_CHECKS", "MERGEABLE", "UNMERGEABLE", "LOCKED"]


@st.composite
def _queue_frames(draw):
    """Open PRs 1..n, each independently flagged queued on its PR record and/or
    present in the queue snapshot — the two separately polled sources."""
    n = draw(st.integers(min_value=0, max_value=14))
    prs, entries = [], []
    for number in range(1, n + 1):
        flagged = draw(st.booleans())
        in_snapshot = draw(st.booleans())
        prs.append(PipelinePr(pr=_pr(
            number=number, in_merge_queue=flagged,
            stage=draw(st.sampled_from(["ci", "ready", "parked"])),
            checks=draw(st.sampled_from(["passing", "failing", "pending"])),
        )))
        if in_snapshot:
            entries.append(QueueEntry(
                pr_number=number, position=len(entries) + 1,
                state=draw(st.sampled_from(_ENTRY_STATES)), enqueued_at=None,
                batch_head_oid=draw(st.sampled_from([None, "a", "b"])),
                group_checks=draw(st.sampled_from([None, "passing", "failing", "pending"])),
                checks_total=None, checks_done=None, eta_seconds=None))
    return prs, entries


@settings(max_examples=200, deadline=None)
@given(_queue_frames())
def test_queue_stage_and_lane_count_the_same_prs_exactly_once(frame):
    prs, entries = frame
    queues = {REPO: entries} if entries else {}
    repo = dashboard_state({REPO: prs}, queues, {}, {}, generated_at="x")["repos"][0]
    lane = _lane_numbers(repo["queue"])
    # Drawn at most once: no PR is in two cars.
    assert all(c == 1 for c in Counter(lane).values()), lane
    queued = {p["number"] for p in repo["prs"] if p["stage"]["stage"] == "queue"}
    # In queue tile (+ queue rows bucketed Failed) == what the lane draws.
    assert queued == set(lane)
    # And a PR either source calls queued is on both.
    sources = {p.pr.number for p in prs if p.pr.in_merge_queue} | {e.pr_number for e in entries}
    assert queued == sources


def _row(number: int, merged_at: str, qa: str | None, prod: str | None):
    return SimpleNamespace(number=number, merged_at=merged_at, qa_live_at=qa,
                           prod_live_at=prod, merge_commit_sha=f"s{number}")


_TS = st.sampled_from([f"2026-07-1{d}T00:00:00Z" for d in range(10)])


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(_TS, st.booleans(), st.booleans()), max_size=12))
def test_configured_repo_deploy_stages_match_the_deploy_block(rows_spec):
    rows = [_row(i + 1, at, at if qa else None, at if prod else None)
            for i, (at, qa, prod) in enumerate(rows_spec)]
    block = compute_repo_deploy(rows, [])
    prs = [PipelinePr(pr=_pr(number=r.number, state="merged", merged=True, stage="merged"))
           for r in rows]
    timelines = {r.number: {"mergedAt": r.merged_at, "qaLiveAt": r.qa_live_at,
                            "prodLiveAt": r.prod_live_at} for r in rows}
    repo = dashboard_state({REPO: prs}, {}, {}, {}, generated_at="x",
                           deploy_ctx={REPO: block},
                           timeline_ctx={REPO: timelines})["repos"][0]
    stages = Counter(p["stage"]["stage"] for p in repo["prs"])
    assert stages["qa-deploy"] == block["awaitingQa"]
    assert stages["awaiting-prod"] == block["awaitingProd"]


_ENVS = st.lists(st.sampled_from(["qa", "production", "admin"]), unique=True, max_size=3)


@settings(max_examples=200, deadline=None)
@given(st.lists(_ENVS, max_size=12))
def test_unconfigured_repo_deploy_stages_are_counted_by_the_backlog(envs_per_pr):
    prs = [PipelinePr(pr=_pr(number=i + 1, state="merged", merged=True, stage="merged",
                             deployed_envs=tuple(envs),
                             deployed="production" in envs))
           for i, envs in enumerate(envs_per_pr)]
    repo = dashboard_state({REPO: prs}, {}, {}, {}, generated_at="x")["repos"][0]
    backlog = repo.get("deployBacklog") or {"awaitingQa": 0, "awaitingProd": 0}
    stages = Counter(p["stage"]["stage"] for p in repo["prs"])
    # qa-deploy is exactly the backlog's awaiting-QA set; an awaiting-prod row is
    # one the backlog counts awaiting prod (it may also count a qa-deploy row
    # there — the backlog is flat 3-way, the stages are disjoint).
    assert stages["qa-deploy"] == backlog["awaitingQa"]
    assert stages["awaiting-prod"] <= backlog["awaitingProd"]


def test_red_ci_outranks_a_conflict_in_the_parked_substate():
    # A red, conflicting PR used to read "conflicting" → "Merged / other" tile,
    # missing from both the Failed tile and the header's PR CI red count.
    s = stage_result(_pr(stage="parked", checks="failing", mergeable="CONFLICTING",
                         merge_state_status="DIRTY"))
    assert s["substate"] == "ci-failed"
    # An advisory red (UNSTABLE: every required check passed) parked for a
    # conflict stays "conflicting" — derive_open_stage didn't park it for CI.
    s = stage_result(_pr(stage="parked", checks="failing", mergeable="CONFLICTING",
                         merge_state_status="UNSTABLE"))
    assert s["substate"] == "conflicting"
    # DIRTY with mergeable still UNKNOWN is a conflict, not an unexplained park.
    s = stage_result(_pr(stage="parked", checks="passing", mergeable="UNKNOWN",
                         merge_state_status="DIRTY"))
    assert s["substate"] == "conflicting"
    # Draft keeps precedence: a WIP PR's red CI isn't an alarm.
    s = stage_result(_pr(stage="parked", checks="failing", is_draft=True, state="draft"))
    assert s["substate"] == "draft"
