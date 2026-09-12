# server/tests/test_deploy_monitor_policy.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.automations import deploy_monitor as dm
from server.github.deployments import Deployment

NOW = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)


def _dep(sha="a" * 40, state="FAILURE", status_age_min=10, created_age_min=12, env="kindash / admin"):
    return Deployment(env=env, sha=sha, ref="main", creator="railway-app",
                      created_at=NOW - timedelta(minutes=created_age_min),
                      status_state=state, status_at=NOW - timedelta(minutes=status_age_min),
                      log_url="https://railway.app/log", env_url="https://admin.example")


def _eval(deps, grace_min=5, stall_min=30):
    return dm.evaluate_env("kindash / admin", deps, now=NOW, grace_min=grace_min, stall_min=stall_min)


def test_contract_surface():
    a = dm.DeployMonitor()
    assert a.id == "deploy-monitor" and a.modes == ("off", "watch", "file")
    assert a.work_class == "maintenance"
    keys = [f.key for f in a.config_schema]
    assert keys == ["enabled_repos", "all_envs", "grace_min", "stall_min", "comment_every_h", "daily_cap"]
    assert a.status(None).level == "idle"


def test_status_levels_from_counts():
    class Ctx:
        deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 1, "stalled": 0, "incidents": 1}
    s = dm.DeployMonitor().status(Ctx())
    assert s.level == "error" and "1 incident" in s.text and "5 envs" in s.text
    Ctx.deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 0, "stalled": 1, "incidents": 1}
    assert dm.DeployMonitor().status(Ctx()).level == "warn"
    Ctx.deploy_monitor_counts = {"repos": 2, "envs": 5, "failing": 0, "stalled": 0, "incidents": 0}
    assert dm.DeployMonitor().status(Ctx()).level == "active"
    Ctx.deploy_monitor_counts = {"repos": 0, "envs": 0, "failing": 0, "stalled": 0, "incidents": 0}
    assert dm.DeployMonitor().status(Ctx()).level == "idle"


def test_empty_is_none():
    assert _eval([]) is None


def test_failing_past_grace_counts_streak():
    v = _eval([_dep(sha="1" * 40), _dep(sha="2" * 40, state="ERROR"), _dep(sha="3" * 40, state="SUCCESS"),
               _dep(sha="4" * 40)])
    assert v.kind == "failing" and v.streak == 2 and v.newest.sha == "1" * 40


def test_failing_inside_grace_is_unknown():
    assert _eval([_dep(status_age_min=2)]).kind == "unknown"


def test_failing_with_no_timestamps_is_failing():
    d = Deployment(env="e", sha="x" * 40, ref=None, creator=None, created_at=None,
                   status_state="FAILURE", status_at=None, log_url=None, env_url=None)
    assert _eval([d]).kind == "failing"


def test_stalled_after_stall_window_else_unknown():
    assert _eval([_dep(state="IN_PROGRESS", created_age_min=45)]).kind == "stalled"
    assert _eval([_dep(state="", created_age_min=45)]).kind == "stalled"
    assert _eval([_dep(state="PENDING", created_age_min=5)]).kind == "unknown"


def test_healthy_states():
    assert _eval([_dep(state="SUCCESS"), _dep(state="FAILURE")]).kind == "healthy"
    assert _eval([_dep(state="INACTIVE")]).kind == "healthy"


def test_unrecognised_state_is_unknown():
    assert _eval([_dep(state="BANANA")]).kind == "unknown"


def test_watched_env_filters_unclassified_unless_all_envs():
    assert dm.watched_env("kindash / admin", {})
    assert dm.watched_env("production", {})
    assert not dm.watched_env("preview-123", {})
    assert dm.watched_env("preview-123", {"all_envs": True})


def test_incident_key_roundtrip():
    key = dm.incident_key("cairnea/KinDash", "kindash / admin#x")
    assert key == "cairnea/KinDash#kindash / admin#x"
    assert dm.split_key(key) == ("cairnea/KinDash", "kindash / admin#x")


def _state(**over):
    base = {"kind": "failing", "sha": "a" * 40, "ref": "main", "creator": "railway-app",
            "first_sha": "b" * 40, "first_seen_at": "2026-09-12T20:23:10+00:00",
            "last_seen_at": "2026-09-12T22:00:00+00:00", "streak": 3,
            "log_url": "https://railway.app/log <!-- x -->", "env_url": "https://admin.example"}
    base.update(over)
    return base


def test_issue_title_and_body():
    t = dm.issue_title("cairnea/KinDash", "kindash / admin", _state())
    assert t == "[deploy-monitor] kindash / admin deploy failing — cairnea/KinDash @ aaaaaaaaaa"
    assert "stalled" in dm.issue_title("o/r", "qa", _state(kind="stalled"))
    body = dm.issue_body("cairnea/KinDash", "kindash / admin", _state(), regression_of=None)
    assert "| Environment | kindash / admin |" in body
    assert "3" in body and "railway-app" in body and "bbbbbbbbbb" in body
    assert "<!--" not in body                       # neutralized
    assert body.rstrip().endswith("Target: `cairnea/KinDash#kindash / admin`")
    assert "Regression of" not in body
    assert "#41" in dm.issue_body("o/r", "qa", _state(), regression_of=41)


def test_issue_body_is_capped():
    from server.github import ci_issues
    body = dm.issue_body("o/r", "qa", _state(log_url="x" * 20000), regression_of=None)
    assert len(body) <= ci_issues.BODY_MAX_CHARS


def test_issue_body_escapes_ref_pipe_once():
    # Finding 4: the 'Newest deployment' cell used to pre-escape `ref` with an
    # inline `_cell` call, and the table join then escaped it AGAIN, so a `|`
    # in a ref rendered double-escaped instead of once.
    body = dm.issue_body("o/r", "qa", _state(ref="feat|x"), regression_of=None)
    assert "feat\\|x" in body
    assert "feat\\\\|x" not in body


def test_comments():
    c = dm.recurrence_comment(_state(streak=4))
    assert "4" in c and "aaaaaaaaaa" in c
    r = dm.recovery_comment("kindash / admin", "c" * 40, NOW)
    assert "cccccccccc" in r and "2026-09-12 22:00 UTC" in r
