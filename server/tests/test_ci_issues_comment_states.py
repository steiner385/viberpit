from __future__ import annotations

import json
import subprocess


def _runner(calls, *, rc=0, stdout=""):
    def run(args, *a, **k):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, rc, stdout, "nope" if rc else "")
    return run


def test_comment_on_issue_posts_and_returns_url():
    from server.github.ci_issues import comment_on_issue
    calls: list[list[str]] = []
    url = comment_on_issue("o/r", 7, "hello",
                           run=_runner(calls, stdout=json.dumps({"html_url": "https://x/c/1"})))
    assert url == "https://x/c/1"
    assert calls[0][:2] == ["api", "repos/o/r/issues/7/comments"]
    assert "body=hello" in calls[0]


def test_comment_on_issue_failure_returns_none():
    from server.github.ci_issues import comment_on_issue
    assert comment_on_issue("o/r", 7, "x", run=_runner([], rc=1)) is None
    def raiser(*a, **k):
        raise OSError("gh missing")
    assert comment_on_issue("o/r", 7, "x", run=raiser) is None


def test_issue_states_one_graphql_call():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    payload = {"data": {"repository": {
        "i1": {"number": 1, "closed": True, "stateReason": "NOT_PLANNED"},
        "i2": {"number": 2, "closed": False, "stateReason": None},
        "i3": None,
        "i4": {"number": 4, "closed": True, "stateReason": "COMPLETED"},
    }}}
    out = issue_states("o/r", [1, 2, 3, 4], run=_runner(calls, stdout=json.dumps(payload)))
    assert out == {1: (True, "not_planned"), 2: (False, None), 4: (True, "completed")}
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]
    query = calls[0][3]
    assert 'repository(owner: "o", name: "r")' in query
    assert "i4: issue(number: 4)" in query


def test_issue_states_empty_and_failure():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    assert issue_states("o/r", [], run=_runner(calls)) == {}
    assert calls == []
    assert issue_states("o/r", [1], run=_runner(calls, rc=1)) == {}
