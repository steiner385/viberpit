from server.api.sessions import _record_to_dict
from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


def test_record_to_dict_includes_prs_when_given():
    prs = [PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")]
    d = _record_to_dict(_rec(), repo="o/r", prs=prs)
    assert d["prs"] == [{
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t", "deployed": False,
        "deployed_envs": [],
        "head_sha": "", "is_draft": False, "merged": False, "updated_at": "",
        "created_at": "", "merged_at": "", "merge_commit_sha": "",
        "checks_detail": [], "check_groups": [], "stage": "", "repo": "",
        "head_ref": "",
        "eta_seconds": None, "eta_percent": None, "eta_overdue": False,
        "eta_low_seconds": None, "eta_high_seconds": None,
        "eta_stale": False, "eta_updated_at": "",
        "mergeable": "UNKNOWN", "merge_state_status": "", "auto_merge_enabled": False,
        "in_merge_queue": False, "touches_workflows": False,
    }]
    assert "pr" not in d


def test_record_to_dict_prs_empty_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["prs"] == []


def test_record_to_dict_pr_includes_check_groups_key():
    row = CheckRow(
        name="build", workflow="CI", status="COMPLETED", conclusion="success",
        duration_seconds=30, required=True, overdue=False,
    )
    group = CheckGroup("head", (row,), 30, 0.008)
    pr = PullRequestInfo(
        7, "https://github.com/o/r/pull/7", "open", "passing", "feat",
        check_groups=(group,),
    )
    d = _record_to_dict(_rec(), repo="o/r", prs=[pr])
    assert d["prs"][0]["check_groups"][0]["kind"] == "head"
