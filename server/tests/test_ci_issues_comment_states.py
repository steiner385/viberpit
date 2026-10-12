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
    # The body carries the automation marker after the text (#2683 review).
    assert any(a.startswith("body=hello") for a in calls[0])


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


def test_issue_states_malformed_repo_returns_empty():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    assert issue_states("just-a-name", [1], run=_runner(calls)) == {}
    assert calls == []


def test_issue_states_truncates_to_100():
    from server.github.ci_issues import issue_states
    calls: list[list[str]] = []
    numbers = list(range(1, 151))
    payload = {"data": {"repository": {
        f"i{n}": {"number": n, "closed": False, "stateReason": None} for n in numbers[:100]
    }}}
    issue_states("o/r", numbers, run=_runner(calls, stdout=json.dumps(payload)))
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]
    query = calls[0][3]
    assert "i100: issue(number: 100)" in query
    assert "i101:" not in query


def test_remove_label_deletes_the_named_label():
    from server.github.ci_issues import remove_label
    calls: list[list[str]] = []
    assert remove_label("o/r", 7, "Parked", run=_runner(calls)) is True
    assert calls == [["api", "-X", "DELETE", "repos/o/r/issues/7/labels/Parked"]]
    assert remove_label("o/r", 7, "parked", run=_runner([], rc=1)) is False
    # A name that would retarget the path is encoded or refused before gh runs.
    calls.clear()
    assert remove_label("o/r", 7, "../../x", run=_runner(calls)) is True
    assert calls == [["api", "-X", "DELETE", "repos/o/r/issues/7/labels/..%2F..%2Fx"]]
    calls.clear()
    assert remove_label("o/r", 7, "..", run=_runner(calls)) is False
    assert remove_label("../x", 7, "parked", run=_runner(calls)) is False
    assert calls == []


def test_label_placer_is_the_actor_of_the_latest_labeled_event():
    from server.github.ci_issues import label_placer
    calls: list[list[str]] = []
    out = "steiner385\nsomeone-else\n"
    assert label_placer("o/r", 7, "parked", run=_runner(calls, stdout=out)) == "someone-else"
    assert calls[0][:3] == ["api", "--paginate", "repos/o/r/issues/7/events"]
    assert label_placer("o/r", 7, "parked", run=_runner([], stdout="")) is None
    assert label_placer("o/r", 7, "parked", run=_runner([], rc=1)) is None


def test_viewer_login_reads_the_ambient_gh_account():
    from server.github.ci_issues import viewer_login
    calls: list[list[str]] = []
    assert viewer_login(run=_runner(calls, stdout="steiner385\n")) == "steiner385"
    assert calls == [["api", "user", "--jq", ".login"]]
    assert viewer_login(run=_runner([], rc=1)) is None
    assert viewer_login(run=_runner([], stdout="\n")) is None
