from server.github.pr_status import (
    PRODUCTION_ENVIRONMENT, PullRequestInfo, build_pr_query, parse_pr_response,
)


def _resp(alias_to_nodes: dict) -> dict:
    return {"data": {a: {"pullRequests": {"nodes": n}} for a, n in alias_to_nodes.items()}}


def test_from_dict_roundtrips_to_dict():
    # from_dict is the inverse of to_dict so the daemon→edge RPC can rebuild a
    # PullRequestInfo from the wire payload.
    pr = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    assert PullRequestInfo.from_dict(pr.to_dict()) == pr


def _pr(number, state, *, draft=False, rollup="SUCCESS", updated="2026-06-22T10:00:00Z"):
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
    assert info == PullRequestInfo(7, "https://github.com/o/r/pull/7", "open", "passing", "PR 7")


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
