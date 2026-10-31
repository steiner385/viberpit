from __future__ import annotations

import json
from types import SimpleNamespace

from server.github import ci_issues


def _runner(calls, *, rc=0, stdout=""):
    def run(args, *a, **k):
        calls.append(args)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
    return run


def test_edit_issue_body_and_add_label_argv():
    calls: list = []
    assert ci_issues.edit_issue_body("o/r", 7, "hello", run=_runner(calls))
    assert calls[0] == ["api", "-X", "PATCH", "repos/o/r/issues/7", "-f", "body=hello"]
    calls.clear()
    assert ci_issues.add_label("o/r", 7, "ci:systemic-red", run=_runner(calls))
    assert calls[0][:2] == ["api", "repos/o/r/labels"]  # _ensure_label first
    assert calls[-1] == ["api", "repos/o/r/issues/7/labels", "-f", "labels[]=ci:systemic-red"]


def test_reopen_issue_argv_and_failure():
    calls: list = []
    assert ci_issues.reopen_issue("o/r", 7, run=_runner(calls))
    assert calls[0] == ["api", "-X", "PATCH", "repos/o/r/issues/7", "-f", "state=open"]
    assert not ci_issues.reopen_issue("o/r", 7, run=_runner([], rc=1))


def test_issue_snapshot_parses_state_body_labels():
    payload = {"state": "open", "body": "b", "labels": [{"name": "a"}, {"name": "ci:x"}]}
    assert ci_issues.issue_snapshot("o/r", 7, run=_runner([], stdout=json.dumps(payload))) == {
        "state": "open", "body": "b", "labels": ["a", "ci:x"]}
    assert ci_issues.issue_snapshot("o/r", 7, run=_runner([], rc=1)) is None
    assert ci_issues.issue_snapshot("o/r", 7, run=_runner([], stdout="not json")) is None
