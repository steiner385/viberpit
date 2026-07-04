"""ci-doctor issue lifecycle via injected gh runner."""
from __future__ import annotations

import json
from types import SimpleNamespace

from server.github.ci_issues import close_issue, create_issue


def test_create_ensures_label_then_files():
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        if args[1].endswith("/labels"):
            return SimpleNamespace(returncode=1, stdout="", stderr="already_exists")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"number": 77}), stderr="")
    assert create_issue("o/r", "t", "b", run=fake_run) == 77
    assert calls[0][1] == "repos/o/r/labels"          # ensure-label first
    assert calls[1][1] == "repos/o/r/issues"
    assert "labels[]=ci-doctor" in " ".join(calls[1])


def test_create_failure_returns_none():
    def fake_run(args, token=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    assert create_issue("o/r", "t", "b", run=fake_run) is None


def test_close_issue():
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    assert close_issue("o/r", 77, run=fake_run) is True
    assert "repos/o/r/issues/77" in calls[0] and "-X" in calls[0]
