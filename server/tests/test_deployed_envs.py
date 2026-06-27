from server.github.pr_status import (
    PullRequestInfo, _active_deploy_dates, _is_deployed_envs, build_population_query,
)


def test_active_deploy_dates_groups_by_env_newest_success():
    node = {"deployments": {"nodes": [
        {"latestEnvironment": "production", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-20T00:00:00Z"}},
        {"latestEnvironment": "production", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-10T00:00:00Z"}},  # older, ignored
        {"latestEnvironment": "staging", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-25T00:00:00Z"}},
        {"latestEnvironment": "qa", "latestStatus": {"state": "FAILURE"},
         "commit": {"committedDate": "2026-06-26T00:00:00Z"}},  # not SUCCESS, ignored
    ]}}
    dates = _active_deploy_dates(node)
    assert dates == {"production": "2026-06-20T00:00:00Z", "staging": "2026-06-25T00:00:00Z"}


def test_is_deployed_envs_membership_by_merged_at():
    pr = {"mergedAt": "2026-06-15T00:00:00Z"}
    dates = {"production": "2026-06-20T00:00:00Z", "staging": "2026-06-10T00:00:00Z"}
    # live in production (merged 06-15 ≤ prod deploy 06-20); NOT staging (06-15 > 06-10)
    assert _is_deployed_envs(pr, "merged", dates) == ("production",)


def test_is_deployed_envs_empty_for_open():
    assert _is_deployed_envs({"mergedAt": None}, "open", {"production": "x"}) == ()


def test_pullrequestinfo_deployed_bool_tracks_production_env():
    pr = PullRequestInfo(number=1, url="u", state="merged", checks="passing", title="t",
                         deployed=True, deployed_envs=("qa", "production"))
    d = pr.to_dict()
    assert d["deployed_envs"] == ["qa", "production"]
    assert d["deployed"] is True
    back = PullRequestInfo.from_dict(d)
    assert back.deployed_envs == ("qa", "production")
    assert back.deployed is True


def test_population_query_unfiltered_deployments_with_latest_environment():
    q = build_population_query("o/r", "2026-06-20")
    assert "latestEnvironment" in q
    assert 'environments: ["production"]' not in q
