"""milestone_refresh — run a ladder's GitHub readings and write ``milestone_measurements``.

The GraphQL payload is the Task 5 fixture (fixtures/milestones/graphql_payload.json),
whose aliases match the ``household`` milestone below; the ``friendly`` milestone's ref
(alias ``m0``) is added to it here. Numbers asserted are the fixture's: refs #20200
CLOSED and #20234 MERGED; search issueCount 1; epic #20191 with 5 sub-issues, 2 closed.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import MilestoneLadder, MilestoneMeasurement, Project, ProjectRepo
from server.pm.milestone_def import parse_ladder, to_json
from server.pm.milestone_refresh import (
    RefreshResult,
    newest_fetched_at,
    refresh_ladder,
)

FIXTURES = Path(__file__).parent / "fixtures" / "milestones"
PRIMARY = "cairnea/KinDash"
NOW = datetime(2026, 11, 5, 12, 0, 0)

LADDER = """
version: 1
title: t
milestones:
  - key: household
    name: Household
    epics: ['#20191']
    gate:
      - key: lane-fixes
        label: L
        measure: {kind: refs_closed, refs: ['#20200', '#20234']}
      - key: p0
        label: P
        measure: {kind: issue_count, query: 'is:issue is:open label:"priority:P0-stability"', op: eq, target: 0}
      - key: subs
        label: S
        measure: {kind: sub_issues}
      - key: nightly
        label: N
        measure: {kind: workflow_streak, workflow: nightly.yml, branch: main, target: 3}
      - key: rubric
        label: R
        measure: {kind: ci_metric, workflow: metrics.yml, metric: gap, op: eq, target: 0}
      - key: setup
        label: M
        measure: {kind: manual}
  - key: friendly
    name: Friendly
    refs: ['#20240']
numbers:
  - key: leaves
    label: leaves closed
    measure: {kind: ladder_leaves}
"""

M0 = {"__typename": "Issue", "number": 20240, "title": "a ref", "state": "OPEN",
      "url": "https://github.com/cairnea/KinDash/issues/20240",
      "repository": {"nameWithOwner": PRIMARY},
      "closedByPullRequestsReferences": {"nodes": []}}

RUNS = [  # newest first
    {"id": 4, "status": "completed", "conclusion": "success", "event": "schedule",
     "updated_at": "2026-11-05T06:00:00Z", "html_url": "https://run/4"},
    {"id": 3, "status": "completed", "conclusion": "success", "event": "push",
     "updated_at": "2026-11-04T18:00:00Z", "html_url": "https://run/3"},
    {"id": 2, "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
     "updated_at": "2026-11-04T06:00:00Z", "html_url": "https://run/2"},
    {"id": 1, "status": "completed", "conclusion": "failure", "event": "schedule",
     "updated_at": "2026-11-03T06:00:00Z", "html_url": "https://run/1"},
]


def _payload() -> dict:
    p = json.loads((FIXTURES / "graphql_payload.json").read_text())
    p["data"]["repo0"]["m0"] = copy.deepcopy(M0)
    return p


async def _seed(text: str = LADDER, *, primary: str = PRIMARY) -> str:
    ladder = parse_ladder(text, primary_repo=primary)
    async with db_session() as db:
        p = Project(slug="kd", name="kd")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=primary, is_primary=True))
        db.add(MilestoneLadder(project_id=p.id, definition=to_json(ladder), title="t"))
        return p.id


async def _rows() -> dict[tuple[str, str], MilestoneMeasurement]:
    async with db_session() as db:
        rows = (await db.execute(select(MilestoneMeasurement))).scalars().all()
        return {(r.scope, r.key): r for r in rows}


class _Gh:
    def __init__(self, *payloads, raises: Exception | None = None):
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []
        self.raises = raises

    async def __call__(self, owner: str, query: str) -> dict:
        self.calls.append((owner, query))
        if self.raises is not None:
            raise self.raises
        return self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]


class _Runs:
    """Behaves like the Actions API: ``event=`` filters server-side, then one page."""

    def __init__(self, runs=None):
        self.runs = RUNS if runs is None else runs
        self.calls: list[tuple] = []

    def __call__(self, repo, workflow, *, branch, event=None, limit=30):
        self.calls.append((repo, workflow, branch, event))
        runs = [r for r in self.runs if event is None or r["event"] == event]
        return copy.deepcopy(runs[:limit])


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


@pytest.mark.asyncio
async def test_refresh_writes_conditions_and_leaves(isolated_db):
    pid = await _seed()
    gh, runs = _Gh(_payload()), _Runs()
    result = await refresh_ladder(pid, graphql=gh, list_runs=runs, now=NOW)
    rows = await _rows()

    assert [owner for owner, _ in gh.calls] == ["cairnea"]
    assert runs.calls == [(PRIMARY, "nightly.yml", "main", "schedule"),
                          (PRIMARY, "nightly.yml", "main", "workflow_dispatch")]

    lane = rows[("household", "lane-fixes")]
    assert lane.value == {"done": 2, "total": 2} and lane.status == "met"
    assert lane.error is None and _naive(lane.fetched_at) == NOW and _naive(lane.measured_at) == NOW

    p0 = rows[("household", "p0")]
    assert p0.value == {"n": 1} and p0.status == "unmet"
    assert p0.target == {"op": "eq", "target": 0}
    assert p0.source_url and p0.source_url.startswith("https://github.com/search?q=")

    subs = rows[("household", "subs")]
    assert subs.value == {"done": 2, "total": 5} and subs.status == "unmet"
    assert subs.source_url == "https://github.com/cairnea/KinDash/issues/20191"

    leaves = rows[("leaves:household", "leaves")]
    assert (leaves.value["done"], leaves.value["total"]) == (2, 5)
    assert [i["number"] for i in leaves.value["items"]] == [20195, 20202, 20203, 29001, 7]
    assert leaves.error is None and _naive(leaves.measured_at) == NOW

    friendly = rows[("leaves:friendly", "leaves")]
    assert friendly.value["done"] == 0 and friendly.value["total"] == 1
    assert friendly.value["items"][0]["number"] == 20240

    numbers = rows[("numbers", "leaves")]
    assert numbers.value == {"done": 2, "total": 6} and numbers.status == "info"

    # The push run is not counted: default events are schedule + workflow_dispatch.
    nightly = rows[("household", "nightly")]
    assert nightly.value == {"n": 2} and nightly.status == "unmet"
    assert _naive(nightly.measured_at) == datetime(2026, 11, 5, 6, 0, 0)
    assert nightly.source_url == "https://run/4"

    assert ("household", "setup") not in rows  # manual: never written by refresh
    assert result == RefreshResult(written=len(rows), errors=1)  # the ci_metric row


@pytest.mark.asyncio
async def test_workflow_streak_events_param_overrides_default(isolated_db):
    text = LADDER.replace("branch: main, target: 3}",
                          "branch: main, target: 3, events: [push, schedule]}")
    pid = await _seed(text)
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    # schedule ok, push ok, (workflow_dispatch filtered), schedule failure -> 2
    nightly = (await _rows())[("household", "nightly")]
    assert nightly.value == {"n": 2}
    text = LADDER.replace("branch: main, target: 3}",
                          "branch: main, target: 3, events: [schedule, push, workflow_dispatch]}")
    async with db_session() as db:
        ladder = (await db.execute(select(MilestoneLadder))).scalar_one()
        ladder.definition = to_json(parse_ladder(text, primary_repo=PRIMARY))
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    nightly = (await _rows())[("household", "nightly")]
    assert nightly.value == {"n": 3} and nightly.status == "met"


@pytest.mark.asyncio
async def test_graphql_failure_marks_errors_keeps_values(isolated_db):
    pid = await _seed()
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    later = NOW + timedelta(minutes=10)
    result = await refresh_ladder(pid, graphql=_Gh(raises=RuntimeError("gh graphql failed: HTTP 502")),
                                  list_runs=_Runs(), now=later)
    rows = await _rows()
    for scope, key, value in [
        ("household", "lane-fixes", {"done": 2, "total": 2}),
        ("household", "p0", {"n": 1}),
        ("household", "subs", {"done": 2, "total": 5}),
        ("numbers", "leaves", {"done": 2, "total": 6}),
    ]:
        row = rows[(scope, key)]
        assert row.value == value, (scope, key)
        assert _naive(row.measured_at) == NOW, (scope, key)  # true age, not the fetch time
        assert _naive(row.fetched_at) == later
        assert row.error and "HTTP 502" in row.error
        assert row.status == "unknown"
    leaves = rows[("leaves:household", "leaves")]
    assert leaves.value["total"] == 5 and len(leaves.value["items"]) == 5
    assert _naive(leaves.measured_at) == NOW and "HTTP 502" in (leaves.error or "")
    # Workflow runs are a separate read and still refresh.
    assert rows[("household", "nightly")].error is None
    # lane-fixes, p0, subs, both leaves rows, numbers.leaves (sums the kept leaves and
    # carries their error) and the ci_metric row.
    assert result.errors == 7


@pytest.mark.asyncio
async def test_per_alias_error_becomes_error_reading_and_partial_leaves(isolated_db):
    text = """
version: 1
title: t
milestones:
  - key: a
    name: A
    epics: ['#1']
    refs: ['#2']
"""
    pid = await _seed(text, primary="o/r")
    leaf = {"number": 10, "title": "x", "url": "u10", "state": "CLOSED",
            "repository": {"nameWithOwner": "o/r"},
            "closedByPullRequestsReferences": {"nodes": []}}
    ok_epic = {"url": "e", "subIssues": {"totalCount": 1, "pageInfo": {"hasNextPage": False},
                                         "nodes": [leaf]}}
    partial = {"data": {"repo0": {"e0": ok_epic, "m0": None}},
               "errors": [{"path": ["repo0", "m0"], "message": "Could not resolve to an Issue"}]}
    await refresh_ladder(pid, graphql=_Gh(partial), list_runs=_Runs(), now=NOW)
    row = (await _rows())[("leaves:a", "leaves")]
    assert [i["number"] for i in row.value["items"]] == [10]
    assert row.value["done"] == 1 and row.value["total"] == 1
    assert "Could not resolve" in row.error
    assert row.measured_at is None  # not every source read: no fresh measurement time

    # Every source failing keeps the previous items.
    failed = {"data": {"repo0": {"e0": None, "m0": None}},
              "errors": [{"path": ["repo0", "e0"], "message": "nope"}]}
    await refresh_ladder(pid, graphql=_Gh(failed), list_runs=_Runs(), now=NOW + timedelta(minutes=1))
    row = (await _rows())[("leaves:a", "leaves")]
    assert [i["number"] for i in row.value["items"]] == [10] and row.error


TWO_SOURCES = """
version: 1
title: t
milestones:
  - key: a
    name: A
    epics: ['#1']
    refs: ['#2']
numbers:
  - key: leaves
    label: leaves closed
    measure: {kind: ladder_leaves}
"""


def _leaf(n: int, state: str) -> dict:
    return {"__typename": "Issue", "number": n, "title": f"t{n}", "url": f"u{n}", "state": state,
            "repository": {"nameWithOwner": "o/r"},
            "closedByPullRequestsReferences": {"nodes": []}}


@pytest.mark.asyncio
async def test_a_partial_failure_keeps_the_previous_leaves(isolated_db):
    """Once a leaves row has a value, one failed source keeps the WHOLE previous value
    (and ladder_leaves its sums) rather than shrinking to the partial union."""
    pid = await _seed(TWO_SOURCES, primary="o/r")
    epic = {"url": "e", "subIssues": {"totalCount": 1, "pageInfo": {"hasNextPage": False},
                                      "nodes": [_leaf(10, "CLOSED")]}}
    ok = {"data": {"repo0": {"e0": epic, "m0": _leaf(2, "OPEN")}}}
    await refresh_ladder(pid, graphql=_Gh(ok), list_runs=_Runs(), now=NOW)
    assert (await _rows())[("leaves:a", "leaves")].value["total"] == 2

    partial = {"data": {"repo0": {"e0": epic, "m0": None}},
               "errors": [{"path": ["repo0", "m0"], "message": "Could not resolve to an Issue"}]}
    await refresh_ladder(pid, graphql=_Gh(partial), list_runs=_Runs(), now=NOW + timedelta(minutes=10))
    rows = await _rows()
    row = rows[("leaves:a", "leaves")]
    assert [i["number"] for i in row.value["items"]] == [10, 2]
    assert (row.value["done"], row.value["total"]) == (1, 2)
    assert "Could not resolve" in row.error and _naive(row.measured_at) == NOW
    numbers = rows[("numbers", "leaves")]
    assert numbers.value == {"done": 1, "total": 2} and numbers.error


@pytest.mark.asyncio
async def test_ci_metric_without_reader_is_not_read(isolated_db):
    """``read_metrics=None`` (callers that predate it) never reads the artifact."""
    pid = await _seed()
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    row = (await _rows())[("household", "rubric")]
    assert row.status == "unknown" and row.error == "not read"
    assert row.value is None and _naive(row.fetched_at) == NOW


@pytest.mark.asyncio
async def test_manual_rows_untouched(isolated_db):
    pid = await _seed()
    async with db_session() as db:
        ladder = (await db.execute(select(MilestoneLadder))).scalar_one()
        db.add(MilestoneMeasurement(
            ladder_id=ladder.id, scope="household", key="setup", status="met",
            value={"met": True}, measured_at=datetime(2026, 1, 1), manual_by="tony",
            manual_evidence="done"))
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    row = (await _rows())[("household", "setup")]
    assert row.value == {"met": True} and row.status == "met" and row.fetched_at is None
    assert row.manual_by == "tony" and _naive(row.measured_at) == datetime(2026, 1, 1)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(isolated_db):
    pid = await _seed()
    result = await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW,
                                  dry_run=True)
    assert result.written > 0 and await _rows() == {}


@pytest.mark.asyncio
async def test_newest_fetched_at_is_the_last_refresh(isolated_db):
    pid = await _seed()
    assert await newest_fetched_at(pid) is None
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(), now=NOW)
    assert _naive(await newest_fetched_at(pid)) == NOW


@pytest.mark.asyncio
async def test_a_page_of_push_runs_cannot_truncate_the_streak(isolated_db):
    """A full API page of newer ``push`` runs would hide every scheduled run if the
    events were filtered after the fetch; the per-event ``event=`` read keeps them."""
    pushes = [{"id": 1000 + i, "status": "completed", "conclusion": "failure", "event": "push",
               "updated_at": f"2026-11-05T{10 + i // 60:02d}:{i % 60:02d}:00Z",
               "html_url": f"https://run/p{i}"} for i in range(120)]
    scheduled = [{"id": i, "status": "completed", "conclusion": "success", "event": "schedule",
                  "updated_at": f"2026-11-0{5 - i}T06:00:00Z", "html_url": f"https://run/s{i}"}
                 for i in range(4)]
    runs = sorted(pushes + scheduled, key=lambda r: r["updated_at"], reverse=True)
    pid = await _seed()
    await refresh_ladder(pid, graphql=_Gh(_payload()), list_runs=_Runs(runs), now=NOW)
    nightly = (await _rows())[("household", "nightly")]
    assert nightly.value == {"n": 4} and nightly.status == "met"
    assert nightly.source_url == "https://run/s0"


def _epic_page(numbers, *, cursor=None, total):
    nodes = [{"number": n, "title": f"t{n}", "url": f"u{n}", "state": "CLOSED" if n % 2 else "OPEN",
              "repository": {"nameWithOwner": "o/r"},
              "closedByPullRequestsReferences": {"nodes": []}} for n in numbers]
    return {"data": {"repo0": {"e0": {"url": "epic", "subIssues": {
        "totalCount": total,
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": nodes}}}}}


EPIC_ONLY = """
version: 1
title: t
milestones:
  - key: a
    name: A
    epics: ['#1']
    gate:
      - key: subs
        label: S
        measure: {kind: sub_issues}
"""


@pytest.mark.asyncio
async def test_pagination_follows_cursor(isolated_db):
    pid = await _seed(EPIC_ONLY, primary="o/r")
    gh = _Gh(_epic_page([1, 2], cursor="c1", total=3), _epic_page([3], total=3))
    await refresh_ladder(pid, graphql=gh, list_runs=_Runs(), now=NOW)
    assert len(gh.calls) == 2 and 'after: "c1"' in gh.calls[1][1]
    rows = await _rows()
    # done is recomputed over the merged pages (1 and 3 closed), not page one's 1.
    assert rows[("a", "subs")].value == {"done": 2, "total": 3}
    assert [i["number"] for i in rows[("leaves:a", "leaves")].value["items"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_pagination_stops_at_ten_pages(isolated_db):
    pid = await _seed(EPIC_ONLY, primary="o/r")
    pages = [_epic_page([i], cursor=f"c{i}", total=99) for i in range(1, 30)]
    gh = _Gh(*pages)
    await refresh_ladder(pid, graphql=gh, list_runs=_Runs(), now=NOW)
    assert len(gh.calls) == 10
    assert len((await _rows())[("leaves:a", "leaves")].value["items"]) == 10


@pytest.mark.asyncio
async def test_ladder_without_github_reads_makes_no_graphql_call(isolated_db):
    text = """
version: 1
title: t
milestones:
  - key: a
    name: A
    gate:
      - key: m
        label: M
        measure: {kind: manual}
"""
    pid = await _seed(text, primary="o/r")
    gh = _Gh(raises=AssertionError("no call expected"))
    await refresh_ladder(pid, graphql=gh, list_runs=_Runs(), now=NOW)
    assert gh.calls == []
    assert (await _rows())[("leaves:a", "leaves")].value == {"done": 0, "total": 0, "items": []}


# ---------------------------------------------------------------- ci_metric (spec §6)

CI_LADDER = """
version: 1
title: t
milestones:
  - key: a
    name: A
    gate:
      - key: rubric
        label: R
        measure: {kind: ci_metric, workflow: metrics.yml, metric: gap, op: eq, target: 0}
numbers:
  - key: gap
    label: gap
    measure: {kind: ci_metric, workflow: metrics.yml, metric: gap}
"""

CI_RUNS = [  # newest first
    {"id": 5, "status": "completed", "conclusion": "failure", "event": "push",
     "updated_at": "2026-11-05T08:00:00Z", "html_url": "https://run/5"},
    {"id": 4, "status": "completed", "conclusion": "success", "event": "push",
     "updated_at": "2026-11-05T06:00:00Z", "html_url": "https://run/4"},
    {"id": 3, "status": "completed", "conclusion": "success", "event": "schedule",
     "updated_at": "2026-11-04T06:00:00Z", "html_url": "https://run/3"},
    {"id": 2, "status": "completed", "conclusion": "success", "event": "schedule",
     "updated_at": "2026-11-03T06:00:00Z", "html_url": "https://run/2"},
    {"id": 1, "status": "completed", "conclusion": "success", "event": "schedule",
     "updated_at": "2026-11-02T06:00:00Z", "html_url": "https://run/1"},
]


def _metrics(**metrics):
    return {"version": 1, "metrics": metrics, "sha": "abc", "generated_at": "2026-11-05T06:00:00Z"}


class _Metrics:
    """``read_metrics(repo, run_id)``: a per-run-id answer (a dict, None, or an exception)."""

    def __init__(self, by_run):
        self.by_run = by_run
        self.calls: list[tuple[str, int]] = []

    def __call__(self, repo, run_id):
        self.calls.append((repo, run_id))
        out = self.by_run.get(run_id)
        if isinstance(out, Exception):
            raise out
        return copy.deepcopy(out)


class _Branch:
    def __init__(self, branch="main"):
        self.branch = branch
        self.calls: list[str] = []

    async def __call__(self, repo):
        self.calls.append(repo)
        return self.branch


async def _ci_refresh(metrics, *, runs=None, text=CI_LADDER, branch=None):
    pid = await _seed(text, primary="o/r")
    list_runs, branch = _Runs(CI_RUNS if runs is None else runs), branch or _Branch()
    await refresh_ladder(pid, graphql=_Gh(raises=AssertionError("no call expected")),
                         list_runs=list_runs, now=NOW, read_metrics=metrics, default_branch=branch)
    return await _rows(), list_runs, branch


@pytest.mark.asyncio
async def test_ci_metric_reads_newest_successful_run(isolated_db):
    metrics = _Metrics({4: _metrics(gap=293), 3: _metrics(gap=0)})
    rows, list_runs, branch = await _ci_refresh(metrics)
    row = rows[("a", "rubric")]
    assert row.status == "unmet" and row.error is None
    assert row.value == {"n": 293.0} and row.target == {"op": "eq", "target": 0}
    assert _naive(row.measured_at) == datetime(2026, 11, 5, 6, 0, 0)
    assert row.source_url == "https://run/4"
    assert list_runs.calls == [("o/r", "metrics.yml", "main", None)]
    assert branch.calls == ["o/r"] and metrics.calls == [("o/r", 4)]


@pytest.mark.asyncio
async def test_ci_metric_branch_param_overrides_default_branch(isolated_db):
    text = CI_LADDER.replace("metric: gap, op", "metric: gap, branch: release, op")
    rows, list_runs, branch = await _ci_refresh(_Metrics({4: _metrics(gap=0)}), text=text)
    assert rows[("a", "rubric")].status == "met"
    assert ("o/r", "metrics.yml", "release", None) in list_runs.calls


@pytest.mark.asyncio
async def test_ci_metric_skips_runs_without_artifact(isolated_db):
    metrics = _Metrics({4: None, 3: _metrics(gap=0)})
    rows, _runs, _b = await _ci_refresh(metrics)
    row = rows[("a", "rubric")]
    assert row.status == "met" and _naive(row.measured_at) == datetime(2026, 11, 4, 6, 0, 0)
    assert metrics.calls == [("o/r", 4), ("o/r", 3)]



@pytest.mark.asyncio
async def test_ci_metric_probes_at_most_three_runs(isolated_db):
    """Three successful runs probed, none with the artifact: run 1 is never read."""
    metrics = _Metrics({1: _metrics(gap=0)})
    rows, _r, _b = await _ci_refresh(metrics)
    row = rows[("a", "rubric")]
    assert row.status == "unknown"
    assert row.error == "no viberpit-metrics artifact in the last 3 successful runs"
    assert [r for _repo, r in metrics.calls] == [4, 3, 2]


@pytest.mark.asyncio
async def test_ci_metric_missing_key_is_unknown_with_error(isolated_db):
    rows, _r, _b = await _ci_refresh(_Metrics({4: _metrics(other=1)}))
    row = rows[("a", "rubric")]
    assert row.status == "unknown" and row.error == "metric 'gap' missing" and row.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["293", True, None, [1]])
async def test_ci_metric_non_numeric_is_unknown(isolated_db, bad):
    rows, _r, _b = await _ci_refresh(_Metrics({4: _metrics(gap=bad)}))
    row = rows[("a", "rubric")]
    assert row.status == "unknown" and row.error == "metric 'gap' is not a number"


@pytest.mark.asyncio
async def test_ci_metric_no_successful_run_is_unknown(isolated_db):
    metrics = _Metrics({})
    rows, _r, _b = await _ci_refresh(metrics, runs=[CI_RUNS[0]])
    row = rows[("a", "rubric")]
    assert row.status == "unknown" and row.error == "no successful run of metrics.yml"
    assert metrics.calls == []


@pytest.mark.asyncio
async def test_ci_metric_bad_metrics_json_is_unknown(isolated_db):
    rows, _r, _b = await _ci_refresh(_Metrics({4: ValueError("metrics.json: bad JSON")}))
    assert rows[("a", "rubric")].error == "metrics.json: bad JSON"


@pytest.mark.asyncio
async def test_ci_metric_artifact_read_failure_does_not_abort_refresh(isolated_db):
    rows, _r, _b = await _ci_refresh(_Metrics({4: RuntimeError("gh: HTTP 404")}))
    row = rows[("a", "rubric")]
    assert row.status == "unknown" and row.error == "metrics artifact read failed"
    assert rows[("leaves:a", "leaves")].error is None


@pytest.mark.asyncio
async def test_ci_metric_runs_read_failure_is_unknown(isolated_db):
    class _Boom(_Runs):
        def __call__(self, *a, **k):
            raise RuntimeError("HTTP 404: workflow not found")
    pid = await _seed(CI_LADDER, primary="o/r")
    await refresh_ladder(pid, graphql=_Gh(raises=AssertionError("no call expected")),
                         list_runs=_Boom(), now=NOW, read_metrics=_Metrics({}),
                         default_branch=_Branch())
    row = (await _rows())[("a", "rubric")]
    assert row.status == "unknown"
    assert row.error == "workflow runs read failed: HTTP 404: workflow not found"


@pytest.mark.asyncio
async def test_ci_metric_workflow_read_once_for_two_conditions(isolated_db):
    metrics = _Metrics({4: _metrics(gap=0)})
    rows, list_runs, branch = await _ci_refresh(metrics)
    assert rows[("a", "rubric")].status == "met"
    assert rows[("numbers", "gap")].value == {"n": 0.0}
    assert len(list_runs.calls) == 1 and len(metrics.calls) == 1 and len(branch.calls) == 1


@pytest.mark.asyncio
async def test_ci_metric_number_is_info(isolated_db):
    rows, _r, _b = await _ci_refresh(_Metrics({4: _metrics(gap=293)}))
    row = rows[("numbers", "gap")]
    assert row.status == "info" and row.value == {"n": 293.0} and row.target is None
    assert _naive(row.measured_at) == datetime(2026, 11, 5, 6, 0, 0)
