from server.api.sessions import _record_to_dict
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


def test_record_to_dict_includes_pr_when_given():
    pr = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    d = _record_to_dict(_rec(), repo="o/r", pr=pr)
    assert d["pr"] == {
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t",
    }


def test_record_to_dict_pr_null_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["pr"] is None
