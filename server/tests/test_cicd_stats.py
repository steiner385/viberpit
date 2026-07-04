"""Pure flake/stability statistics."""
from __future__ import annotations

from server.automations.cicd.stats import check_stats, test_flake_pcts
from server.automations.cicd.types import JobObs, TestObs

DAY = 86400.0


def _t(test_id, sha, outcome, branch="feat"):
    return TestObs(test_id=test_id, head_sha=sha, head_branch=branch, outcome=outcome)


def test_test_flake_pcts_same_sha_pass_and_fail():
    obs = [_t("t::a", "s1", "failed"), _t("t::a", "s1", "passed"),   # flaky sha
           _t("t::a", "s2", "passed"),                               # clean sha
           _t("t::b", "s1", "passed"), _t("t::b", "s2", "passed")]   # never flaky
    out = test_flake_pcts(obs)
    assert out["t::a"] == (50.0, 2)
    assert out["t::b"] == (0.0, 2)


def test_test_flake_skipped_ignored():
    out = test_flake_pcts([_t("t::a", "s1", "skipped"), _t("t::a", "s1", "failed")])
    assert out["t::a"] == (0.0, 1)  # fail-only sha: a true failure, not a flake


def _j(check, sha, conclusion, ts, branch="feat", dur=60.0):
    return JobObs(check=check, head_sha=sha, head_branch=branch,
                  conclusion=conclusion, duration_s=dur, created_ts=ts)


def test_check_stats_core_metrics():
    now = 100 * DAY
    jobs = [
        _j("e2e", "s1", "failure", now - 30 * DAY), _j("e2e", "s1", "success", now - 30 * DAY),
        _j("e2e", "s2", "success", now - 20 * DAY, dur=120.0),
        _j("e2e", "s3", "failure", now - 1 * DAY, branch="master"),  # unexplained master failure
    ]
    st = check_stats(jobs, default_branch="master", now_ts=now, demote_window_days=7.0)["e2e"]
    assert st.runs_total == 4
    assert 28.9 < st.window_days < 29.1
    assert round(st.flake_pct, 1) == 33.3          # 1 flaky sha of 3
    assert st.default_branch_failures == 1
    assert st.p95_duration_s == 120.0
    assert st.recent_flake_pct == 0.0              # the recent sha s3 is fail-only


def test_check_stats_flake_rising():
    now = 100 * DAY
    jobs = []
    for i, sha in enumerate(["a", "b", "c", "d"]):
        ts = now - (40 - i * 10) * DAY
        jobs.append(_j("e2e", sha, "success", ts))
        if i >= 2:  # later half both passes and fails -> rising
            jobs.append(_j("e2e", sha, "failure", ts))
    st = check_stats(jobs, default_branch="master", now_ts=now, demote_window_days=7.0)["e2e"]
    assert st.flake_rising is True
