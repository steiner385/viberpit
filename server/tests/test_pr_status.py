from datetime import datetime, timezone, timedelta

from server.github.pr_status import (
    PRODUCTION_ENVIRONMENT, CheckDetail, PullRequestInfo, build_pr_query, parse_pr_response,
)


def _resp(alias_to_nodes: dict) -> dict:
    return {"data": {a: {"pullRequests": {"nodes": n}} for a, n in alias_to_nodes.items()}}


def test_from_dict_roundtrips_to_dict():
    # from_dict is the inverse of to_dict so the daemon→edge RPC can rebuild a
    # PullRequestInfo from the wire payload — including the `deployed` flag, which
    # would otherwise silently drop across the remote boundary.
    pr = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    assert PullRequestInfo.from_dict(pr.to_dict()) == pr
    deployed = PullRequestInfo(9, "u", "merged", "passing", "t", deployed=True)
    assert PullRequestInfo.from_dict(deployed.to_dict()) == deployed
    assert PullRequestInfo.from_dict(deployed.to_dict()).deployed is True


def test_stage_field_roundtrips():
    """stage field survives to_dict → from_dict (RPC round-trip)."""
    pr = PullRequestInfo(42, "https://github.com/o/r/pull/42", "open", "passing", "t",
                         stage="ready")
    roundtripped = PullRequestInfo.from_dict(pr.to_dict())
    assert roundtripped.stage == "ready"
    # Default stage is empty string
    pr_no_stage = PullRequestInfo(43, "u", "open", "passing", "t")
    assert pr_no_stage.stage == ""
    assert PullRequestInfo.from_dict(pr_no_stage.to_dict()).stage == ""
    # Absent key in wire dict → default empty string
    d = pr.to_dict()
    del d["stage"]
    assert PullRequestInfo.from_dict(d).stage == ""


def _pr(number, state, *, draft=False, rollup="SUCCESS", updated="2026-06-22T10:00:00Z"):
    # Non-merged PRs only — `mergedAt` is absent here; use `_merged_pr` for MERGED-state cases.
    commit = {"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}
    return {
        "number": number, "url": f"https://github.com/o/r/pull/{number}",
        "title": f"PR {number}", "state": state, "isDraft": draft,
        "updatedAt": updated, "commits": {"nodes": [commit]},
    }


def test_build_query_empty_is_blank():
    assert build_pr_query([]) == ""


def test_build_query_aliases_each_pair():
    q = build_pr_query([("steiner385/cc-account-switcher", "ccswitch/foo-abc")])
    assert "r0: repository(" in q
    assert '"steiner385"' in q and '"cc-account-switcher"' in q
    assert '"ccswitch/foo-abc"' in q
    assert "statusCheckRollup" in q


def test_parse_open_passing():
    pairs = [("o/r", "br")]
    out = parse_pr_response(_resp({"r0": [_pr(7, "OPEN", rollup="SUCCESS")]}), pairs)
    info = out[("o/r", "br")]
    assert info == PullRequestInfo(
        7, "https://github.com/o/r/pull/7", "open", "passing", "PR 7",
        updated_at="2026-06-22T10:00:00Z",
        stage="ready",
    )


def test_parse_state_classification():
    pairs = [("o/r", "br")]
    draft = parse_pr_response(_resp({"r0": [_pr(1, "OPEN", draft=True)]}), pairs)[("o/r", "br")]
    merged = parse_pr_response(_resp({"r0": [_pr(2, "MERGED")]}), pairs)[("o/r", "br")]
    closed = parse_pr_response(_resp({"r0": [_pr(3, "CLOSED")]}), pairs)[("o/r", "br")]
    assert (draft.state, merged.state, closed.state) == ("draft", "merged", "closed")


def test_parse_checks_classification():
    pairs = [("o/r", "br")]
    def checks(rollup):
        return parse_pr_response(_resp({"r0": [_pr(1, "OPEN", rollup=rollup)]}), pairs)[("o/r", "br")].checks
    assert checks("SUCCESS") == "passing"
    assert checks("FAILURE") == "failing"
    assert checks("ERROR") == "failing"
    assert checks("PENDING") == "pending"
    assert checks(None) == "none"


def test_parse_picks_most_relevant_pr():
    # An open PR outranks a merged one even if the merged is newer.
    pairs = [("o/r", "br")]
    nodes = [
        _pr(2, "MERGED", updated="2026-06-22T12:00:00Z"),
        _pr(1, "OPEN", updated="2026-06-22T09:00:00Z"),
    ]
    out = parse_pr_response(_resp({"r0": nodes}), pairs)
    assert out[("o/r", "br")].number == 1


def test_parse_no_prs_omits_key():
    out = parse_pr_response(_resp({"r0": []}), [("o/r", "br")])
    assert ("o/r", "br") not in out


def test_parse_tolerates_missing_alias():
    out = parse_pr_response({"data": {}}, [("o/r", "br")])
    assert out == {}


def _resp_full(alias: str, pr_nodes: list, deploy_nodes: list) -> dict:
    return {"data": {alias: {
        "pullRequests": {"nodes": pr_nodes},
        "deployments": {"nodes": deploy_nodes},
    }}}


def _merged_pr(number, *, merged_at):
    pr = _pr(number, "MERGED")
    pr["mergedAt"] = merged_at
    return pr


def _deploy(commit_date, *, status="SUCCESS"):
    return {
        "commitOid": "deadbeef",
        "latestStatus": {"state": status} if status else None,
        "commit": {"committedDate": commit_date},
    }


def test_build_query_includes_deployments_and_merged_at():
    q = build_pr_query([("o/r", "br")])
    assert "deployments(environments:" in q
    assert PRODUCTION_ENVIRONMENT in q
    assert "mergedAt" in q


def test_deployed_true_when_merged_before_active_deploy():
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")],
        [_deploy("2026-06-22T12:00:00Z")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is True


def test_not_deployed_when_merged_after_active_deploy():
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T13:00:00Z")],
        [_deploy("2026-06-22T12:00:00Z")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_not_deployed_when_no_deployments():
    pairs = [("o/r", "br")]
    resp = _resp_full("r0", [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")], [])
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_not_deployed_when_pr_open():
    pairs = [("o/r", "br")]
    resp = _resp_full("r0", [_pr(5, "OPEN")], [_deploy("2026-06-22T12:00:00Z")])
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_not_deployed_when_pr_closed():
    pairs = [("o/r", "br")]
    resp = _resp_full("r0", [_pr(5, "CLOSED")], [_deploy("2026-06-22T12:00:00Z")])
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_active_deploy_ignores_non_success_status():
    # Newest deploy is still in-progress; the older SUCCESS one is the active commit.
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")],
        [_deploy("2026-06-22T14:00:00Z", status="IN_PROGRESS"),
         _deploy("2026-06-22T11:00:00Z", status="SUCCESS")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is True


def test_deployed_defaults_false_in_dict():
    info = PullRequestInfo(1, "u", "open", "none", "t")
    assert info.to_dict()["deployed"] is False


def test_parse_populates_parity_fields():
    pairs = [("o/r", "br")]
    node = {
        "number": 7, "url": "https://github.com/o/r/pull/7", "title": "PR 7",
        "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-22T10:00:00Z",
        "headRefOid": "deadbeef",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "state": "PENDING",
            "contexts": {"nodes": [
                {"__typename": "CheckRun", "name": "ci", "conclusion": "SUCCESS"},
                {"__typename": "StatusContext", "context": "deploy", "state": "PENDING"},
            ]},
        }}}]},
    }
    out = parse_pr_response(_resp({"r0": [node]}), pairs)[("o/r", "br")]
    assert out.head_sha == "deadbeef"
    assert out.is_draft is False and out.merged is False
    assert out.updated_at == "2026-06-22T10:00:00Z"
    assert CheckDetail(name="ci", conclusion="success") in out.checks_detail
    assert CheckDetail(name="deploy", conclusion="pending") in out.checks_detail


def _pr_with_merge_state(number, state, *, draft=False, rollup="SUCCESS",
                          merge_state_status=None, merged_at=None,
                          updated="2026-06-22T10:00:00Z"):
    """Build a PR node with optional mergeStateStatus and mergedAt."""
    commit = {"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}
    node = {
        "number": number, "url": f"https://github.com/o/r/pull/{number}",
        "title": f"PR {number}", "state": state, "isDraft": draft,
        "updatedAt": updated, "commits": {"nodes": [commit]},
    }
    if merge_state_status is not None:
        node["mergeStateStatus"] = merge_state_status
    if merged_at is not None:
        node["mergedAt"] = merged_at
    return node


def test_stage_computed_for_open_prs():
    """parse_pr_response sets stage correctly for open PRs in all three stage buckets."""
    pairs = [("o/r", "br")]

    # passing + clean → ready
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(1, "OPEN", rollup="SUCCESS", merge_state_status="CLEAN")
    ]}), pairs)
    assert out[("o/r", "br")].stage == "ready"

    # pending → ci
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(2, "OPEN", rollup="PENDING")
    ]}), pairs)
    assert out[("o/r", "br")].stage == "ci"

    # failing → parked
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(3, "OPEN", rollup="FAILURE")
    ]}), pairs)
    assert out[("o/r", "br")].stage == "parked"

    # draft → parked
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(4, "OPEN", draft=True, rollup="SUCCESS")
    ]}), pairs)
    assert out[("o/r", "br")].stage == "parked"

    # passing + DIRTY → parked
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(5, "OPEN", rollup="SUCCESS", merge_state_status="DIRTY")
    ]}), pairs)
    assert out[("o/r", "br")].stage == "parked"


def test_stage_computed_for_merged_pr():
    """parse_pr_response sets stage='merged' for a recently merged PR."""
    pairs = [("o/r", "br")]
    # Merged 2 days ago — within 7-day retention window → 'merged'
    recent_merged_at = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(6, "MERGED", merged_at=recent_merged_at)
    ]}), pairs)
    assert out[("o/r", "br")].stage == "merged"


def test_stage_empty_for_old_merged_pr():
    """parse_pr_response sets stage='' for a merged PR outside the retention window."""
    pairs = [("o/r", "br")]
    # Merged 10 days ago — outside 7-day retention window → ''
    old_merged_at = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = parse_pr_response(_resp({"r0": [
        _pr_with_merge_state(7, "MERGED", merged_at=old_merged_at)
    ]}), pairs)
    assert out[("o/r", "br")].stage == ""


def test_pull_request_info_parity_fields_roundtrip():
    pr = PullRequestInfo(
        number=9, url="u", state="open", checks="passing", title="t",
        head_sha="abc123", is_draft=False, merged=False,
        updated_at="2026-06-22T10:00:00Z",
        checks_detail=(CheckDetail(name="ci", conclusion="success"),),
    )
    back = PullRequestInfo.from_dict(pr.to_dict())
    assert back == pr
    assert back.head_sha == "abc123"
    assert back.checks_detail[0].name == "ci"
    # Defaults still round-trip (older payloads without the new keys):
    legacy = PullRequestInfo.from_dict(
        {"number": 1, "url": "u", "state": "open", "checks": "none", "title": "t"}
    )
    assert legacy.head_sha == "" and legacy.checks_detail == ()


def test_pull_request_info_repo_round_trips():
    info = PullRequestInfo(
        number=7, url="https://github.com/o/n/pull/7", state="open",
        checks="passing", title="t", repo="o/n",
    )
    d = info.to_dict()
    assert d["repo"] == "o/n"
    assert PullRequestInfo.from_dict(d).repo == "o/n"


def test_pull_request_info_repo_defaults_empty_for_old_payload():
    # An old daemon payload has no "repo" key — from_dict must tolerate it.
    d = {"number": 1, "url": "u", "state": "open", "checks": "none"}
    assert PullRequestInfo.from_dict(d).repo == ""


from server.github.pr_status import build_population_query, parse_population_response


def test_build_population_query_scopes_to_repo_and_date():
    q = build_population_query("steiner385/cc-account-switcher", "2026-06-18")
    assert 'repository(owner: "steiner385", name: "cc-account-switcher")' in q
    assert "pullRequests(states: OPEN" in q
    assert 'is:pr is:merged merged:>=2026-06-18' in q
    assert "statusCheckRollup" in q  # shared node fields are present


def test_parse_population_response_sets_repo_and_buckets_open_and_merged():
    response = {"data": {
        "repo": {
            "deployments": {"nodes": []},
            "pullRequests": {"nodes": [
                {"number": 10, "url": "u10", "title": "open one", "state": "OPEN",
                 "isDraft": False, "updatedAt": "2026-06-25T00:00:00Z", "mergedAt": None,
                 "headRefOid": "abc", "mergeStateStatus": "CLEAN",
                 "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS",
                   "contexts": {"nodes": []}}}}]}},
            ]},
        },
        "merged": {"nodes": [
            {"number": 9, "url": "u9", "title": "merged one", "state": "MERGED",
             "isDraft": False, "updatedAt": "2026-06-24T00:00:00Z",
             "mergedAt": "2026-06-24T00:00:00Z", "headRefOid": "def",
             "mergeStateStatus": None, "commits": {"nodes": []}},
        ]},
    }}
    prs = parse_population_response(response, "o/n")
    by_num = {p.number: p for p in prs}
    assert by_num[10].repo == "o/n"
    assert by_num[10].stage == "ready"          # open, passing, clean → ready
    assert by_num[10].state == "open"
    assert by_num[9].state == "merged"
    assert by_num[9].stage == "merged"          # merged within retention
