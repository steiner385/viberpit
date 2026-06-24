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
