"""milestone_github — batched GraphQL readings for a ladder, and workflow streaks.

The recorded payload (fixtures/milestones/graphql_payload.json) is the live
2026-11-05 response for the mini ladder below, trimmed, with two synthetic leaves.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from server.pm.milestone_def import Ref, parse_ladder
from server.pm.milestone_github import (
    Target,
    build_ladder_query,
    epic_reading_key,
    ref_reading_key,
    expand_query_macros,
    parse_ladder_payload,
    streak_from_runs,
)

PRIMARY = "cairnea/KinDash"
NOW = datetime(2026, 11, 5, 12, 0, 0)
FIXTURES = Path(__file__).parent / "fixtures" / "milestones"

MINI = """
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
"""


def _mini():
    return parse_ladder(MINI, primary_repo=PRIMARY)


def _payload() -> dict:
    return json.loads((FIXTURES / "graphql_payload.json").read_text())


def _parse(payload=None):
    _, targets = build_ladder_query(_mini(), primary_repo=PRIMARY, now=NOW)
    return parse_ladder_payload(payload or _payload(), targets, now=NOW)


# ---------------------------------------------------------------- query building


def test_expand_query_macros():
    assert expand_query_macros("created:>={now-7d} is:open", NOW) == "created:>=2026-10-29 is:open"
    assert expand_query_macros("no macro", NOW) == "no macro"


def test_query_groups_refs_by_repo_and_expands_macros():
    ladder = parse_ladder(
        """
version: 1
title: t
milestones:
  - key: a
    name: A
    epics: ['#1', 'other/repo#5']
    gate:
      - key: refs
        label: R
        measure: {kind: refs_closed, refs: ['#10', 'other/repo#11', '#12']}
      - key: recent
        label: C
        measure: {kind: issue_count, query: 'is:issue created:>={now-7d}', op: eq, target: 0}
      - key: scoped
        label: X
        measure: {kind: issue_count, query: 'repo:x/y is:open', op: eq, target: 0}
  - key: b
    name: B
    epics: ['#1']
    gate:
      - key: subs
        label: S
        measure: {kind: sub_issues, epic: '#1'}
""",
        primary_repo="o/r",
    )
    doc, targets = build_ladder_query(ladder, primary_repo="o/r", now=datetime(2026, 11, 5))
    assert "created:>=2026-10-29" in doc
    assert "repo:o/r is:issue created:>=2026-10-29" in doc
    # A query that names its own repo: is left alone.
    assert '"repo:x/y is:open"' in doc
    # Two repository groups, one per repo, each with its refs and epics.
    assert doc.count("repository(owner:") == 2
    assert 'repository(owner: "o", name: "r")' in doc
    assert 'repository(owner: "other", name: "repo")' in doc
    refs = {a: t for a, t in targets.items() if t.kind == "ref"}
    assert {t.ref for t in refs.values()} == {Ref("o/r", 10), Ref("other/repo", 11), Ref("o/r", 12)}
    assert all(t.reads == (("a", "refs"),) for t in refs.values())
    assert sorted(refs) == ["r0", "r1", "r2"]
    # Epic #1 is referenced by both milestones and a sub_issues condition: fetched once.
    epics = {a: t for a, t in targets.items() if t.kind == "epic"}
    assert sorted(t.ref.key for t in epics.values() if t.ref) == ["o/r#1", "other/repo#5"]
    assert doc.count("issue(number: 1)") == 1
    searches = {a: t for a, t in targets.items() if t.kind == "search"}
    assert {(t.scope, t.key) for t in searches.values()} == {("a", "recent"), ("a", "scoped")}
    assert "includeClosedPrs: false" in doc


def test_query_escapes_quotes_and_backslashes():
    doc, _ = build_ladder_query(_mini(), primary_repo=PRIMARY, now=NOW)
    assert r'"repo:cairnea/KinDash is:issue is:open label:\"priority:P0-stability\""' in doc
    ladder = parse_ladder(
        r"""
version: 1
title: t
numbers:
  - key: odd
    label: O
    measure: {kind: issue_count, query: 'in:title "a\b"'}
""",
        primary_repo="o/r",
    )
    doc, _ = build_ladder_query(ladder, primary_repo="o/r", now=NOW)
    assert r'"repo:o/r in:title \"a\\b\""' in doc


def test_negated_repo_qualifier_keeps_primary_scope():
    ladder = parse_ladder(
        """
version: 1
title: t
numbers:
  - key: mine
    label: M
    measure: {kind: issue_count, query: 'is:open -repo:o/x'}
""",
        primary_repo="o/r",
    )
    doc, _ = build_ladder_query(ladder, primary_repo="o/r", now=NOW)
    assert '"repo:o/r is:open -repo:o/x"' in doc


def test_numbers_search_scope_and_repo_param():
    ladder = parse_ladder(
        """
version: 1
title: t
numbers:
  - key: bugs
    label: B
    measure: {kind: issue_count, query: 'is:open', repo: 'x/y'}
""",
        primary_repo="o/r",
    )
    doc, targets = build_ladder_query(ladder, primary_repo="o/r", now=NOW)
    assert '"repo:x/y is:open"' in doc
    assert targets["s0"] == Target(kind="search", scope="numbers", key="bugs", ref=None, query="repo:x/y is:open")


def test_cursors_build_a_continuation_query_for_those_epics_only():
    _, first = build_ladder_query(_mini(), primary_repo=PRIMARY, now=NOW)
    doc, targets = build_ladder_query(_mini(), primary_repo=PRIMARY, now=NOW, cursors={"e0": "Y3Vy"})
    assert set(targets) == {"e0"} and targets["e0"] == first["e0"]
    assert 'after: "Y3Vy"' in doc
    assert "search(" not in doc and "issueOrPullRequest" not in doc


# ---------------------------------------------------------------- payload parsing


def test_parse_counts_and_states():
    readings, leaves, cursors = _parse()
    refs = readings[("household", "lane-fixes")]
    assert refs.error is None
    assert refs.value == {"done": 2, "total": 2}  # CLOSED issue + MERGED PR
    assert refs.measured_at == NOW
    p0 = readings[("household", "p0")]
    assert p0.value == {"n": 1} and p0.measured_at == NOW and p0.error is None
    assert p0.source_url is not None
    assert p0.source_url.startswith("https://github.com/search?q=")
    assert p0.source_url.endswith("&type=issues")
    assert "repo%3Acairnea%2FKinDash" in p0.source_url
    epic = readings[epic_reading_key(Ref(PRIMARY, 20191))]
    assert epic.value == {"done": 2, "total": 5}
    assert epic.source_url == "https://github.com/cairnea/KinDash/issues/20191"
    assert [x["number"] for x in leaves["cairnea/KinDash#20191"]] == [20195, 20202, 20203, 29001, 7]
    assert cursors == {}


def test_leaf_in_flight_only_with_open_pr():
    _, leaves, _ = _parse()
    by_n = {x["number"]: x for x in leaves["cairnea/KinDash#20191"]}
    # Closed issues keep no PRs: their MERGED closing PRs are filtered out.
    assert by_n[20195]["state"] == "CLOSED" and by_n[20195]["prs"] == []
    assert by_n[20202]["prs"] == []
    # Only the OPEN PR survives; the MERGED one alongside it is dropped.
    assert by_n[29001]["prs"] == [{"number": 29002, "url": "https://github.com/cairnea/KinDash/pull/29002"}]


def test_missing_alias_is_error_reading():
    payload = _payload()
    del payload["data"]["s0"]
    payload["data"]["repo0"]["r1"] = None
    payload["data"]["repo0"]["e0"] = None
    payload["errors"] = [{"path": ["repo0", "e0"], "message": "Could not resolve to an Issue with the number of 20191."}]
    readings, leaves, _ = _parse(payload)
    assert readings[("household", "p0")].error == "not found or no access"
    assert readings[("household", "p0")].value is None
    lane = readings[("household", "lane-fixes")]
    assert lane.value is None and lane.error is not None
    assert "cairnea/KinDash#20234" in lane.error
    epic = readings[epic_reading_key(Ref(PRIMARY, 20191))]
    assert epic.value is None
    assert epic.error == "Could not resolve to an Issue with the number of 20191."
    assert "cairnea/KinDash#20191" not in leaves


def test_missing_repository_group_errors_every_alias_inside():
    payload = _payload()
    payload["data"]["repo0"] = None
    readings, _, _ = _parse(payload)
    assert readings[("household", "lane-fixes")].error is not None
    assert readings[epic_reading_key(Ref(PRIMARY, 20191))].error == "not found or no access"
    assert readings[("household", "p0")].error is None


def test_pagination_cursor_returned_when_has_next_page():
    payload = _payload()
    payload["data"]["repo0"]["e0"]["subIssues"]["pageInfo"] = {"hasNextPage": True, "endCursor": "NQ"}
    payload["data"]["repo0"]["e0"]["subIssues"]["totalCount"] = 150
    readings, leaves, cursors = _parse(payload)
    assert cursors == {"e0": "NQ"}
    assert len(leaves["cairnea/KinDash#20191"]) == 5
    # A partial page never over-counts: done is from the fetched leaves, total is the full count.
    assert readings[epic_reading_key(Ref(PRIMARY, 20191))].value == {"done": 2, "total": 150}


def test_cross_repo_leaf_keeps_its_repo():
    _, leaves, _ = _parse()
    by_n = {x["number"]: x for x in leaves["cairnea/KinDash#20191"]}
    assert by_n[7]["repo"] == "cairnea/kindash-infra"
    assert by_n[7]["url"] == "https://github.com/cairnea/kindash-infra/issues/7"
    assert by_n[20202]["repo"] == "cairnea/KinDash"
    assert set(by_n[20202]) == {"repo", "number", "title", "url", "state", "prs"}


# ---------------------------------------------------------------- workflow streaks


def _run(conclusion, updated, *, status="completed", rid=0):
    return {"id": rid, "status": status, "conclusion": conclusion, "event": "schedule",
            "updated_at": updated, "html_url": f"https://github.com/o/r/actions/runs/{rid}"}


def test_streak_ignores_skipped_and_cancelled():
    runs = [
        _run("success", "2026-11-05T06:00:00Z", rid=5),
        _run("skipped", "2026-11-04T06:00:00Z", rid=4),
        _run("success", "2026-11-03T06:00:00Z", rid=3),
        _run("failure", "2026-11-02T06:00:00Z", rid=2),
        _run("success", "2026-11-01T06:00:00Z", rid=1),
    ]
    count, measured_at, url = streak_from_runs(runs)
    assert count == 2
    assert measured_at == datetime(2026, 11, 5, 6, 0, 0)
    assert url == "https://github.com/o/r/actions/runs/5"


def test_streak_skips_in_progress_and_null_conclusions():
    runs = [
        _run(None, "2026-11-06T06:00:00Z", status="in_progress", rid=9),
        _run("cancelled", "2026-11-05T07:00:00Z", rid=8),
        _run("success", "2026-11-05T06:00:00Z", rid=7),
    ]
    assert streak_from_runs(runs) == (1, datetime(2026, 11, 5, 6, 0, 0), "https://github.com/o/r/actions/runs/7")


def test_streak_zero_when_newest_failed():
    runs = [_run("failure", "2026-11-05T06:00:00Z", rid=2), _run("success", "2026-11-04T06:00:00Z", rid=1)]
    assert streak_from_runs(runs) == (0, datetime(2026, 11, 5, 6, 0, 0), "https://github.com/o/r/actions/runs/2")


def test_streak_empty():
    assert streak_from_runs([]) == (0, None, None)
    assert streak_from_runs([_run("skipped", "2026-11-05T06:00:00Z")]) == (0, None, None)


def test_parse_does_not_mutate_payload():
    payload = _payload()
    before = copy.deepcopy(payload)
    _parse(payload)
    assert payload == before


# ---------------------------------------------------------------- milestone refs as leaves

MREF = """
version: 1
title: t
milestones:
  - key: a
    name: A
    refs: ['#5', '#6']
    gate:
      - key: closed
        label: C
        measure: {kind: refs_closed, refs: ['#6', '#7']}
  - key: b
    name: B
    refs: ['#5']
"""


def test_milestone_refs_are_fetched_once_as_leaves_and_serve_refs_closed():
    ladder = parse_ladder(MREF, primary_repo="o/r")
    doc, targets = build_ladder_query(ladder, primary_repo="o/r", now=NOW)
    # #5 named by two milestones, #6 by a milestone and a refs_closed: each fetched once.
    assert doc.count("issueOrPullRequest(number: 5)") == 1
    assert doc.count("issueOrPullRequest(number: 6)") == 1
    assert targets["m0"] == Target("mref", *ref_reading_key(Ref("o/r", 5)), Ref("o/r", 5))
    assert targets["m1"].ref == Ref("o/r", 6) and targets["m1"].reads == (("a", "closed"),)
    assert targets["r0"].kind == "ref" and targets["r0"].ref == Ref("o/r", 7)
    assert targets["r0"].reads == (("a", "closed"),)
    # The milestone refs carry the leaf fields; the plain refs_closed ref does not.
    m_line = next(x for x in doc.splitlines() if x.strip().startswith("m1:"))
    r_line = next(x for x in doc.splitlines() if x.strip().startswith("r0:"))
    assert "closedByPullRequestsReferences" in m_line and "title" in m_line
    assert "title" not in r_line

    payload = {"data": {"repo0": {
        "m0": {"__typename": "Issue", "number": 5, "title": "Five", "url": "https://github.com/o/r/issues/5",
               "state": "OPEN", "repository": {"nameWithOwner": "o/r"},
               "closedByPullRequestsReferences": {"nodes": [
                   {"number": 50, "url": "https://github.com/o/r/pull/50", "state": "OPEN"}]}},
        "m1": {"__typename": "PullRequest", "number": 6, "title": "Six", "url": "https://github.com/o/r/pull/6",
               "state": "MERGED", "repository": {"nameWithOwner": "o/r"}},
        "r0": {"__typename": "Issue", "state": "OPEN", "url": "https://github.com/o/r/issues/7"},
    }}}
    readings, leaves, _ = parse_ladder_payload(payload, targets, now=NOW)
    assert leaves["o/r#5"] == [{"repo": "o/r", "number": 5, "title": "Five", "url": "https://github.com/o/r/issues/5",
                                "state": "OPEN", "prs": [{"number": 50, "url": "https://github.com/o/r/pull/50"}]}]
    # A PR leaf: MERGED is done, and it has no closing PRs.
    assert leaves["o/r#6"] == [{"repo": "o/r", "number": 6, "title": "Six", "url": "https://github.com/o/r/pull/6",
                                "state": "MERGED", "prs": []}]
    assert readings[ref_reading_key(Ref("o/r", 6))].value == {"done": 1, "total": 1}
    assert readings[ref_reading_key(Ref("o/r", 5))].value == {"done": 0, "total": 1}
    closed = readings[("a", "closed")]
    assert closed.value == {"done": 1, "total": 2}
    assert closed.source_url == "https://github.com/o/r/issues/7"

    payload["data"]["repo0"]["m1"] = None
    readings, leaves, _ = parse_ladder_payload(payload, targets, now=NOW)
    assert "o/r#6" not in leaves
    assert readings[ref_reading_key(Ref("o/r", 6))].error == "not found or no access"
    assert readings[("a", "closed")].error == "o/r#6: not found or no access"
