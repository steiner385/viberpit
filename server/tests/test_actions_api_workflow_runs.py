"""actions_api.list_workflow_runs — one workflow's runs on a branch, via an injected runner."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from viberpit import gitrepos
from server.github.actions_api import list_workflow_runs


def _ok(payload) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def _raw(rid, conclusion, *, head="o/r"):
    return {
        "id": rid, "status": "completed", "conclusion": conclusion, "event": "schedule",
        "updated_at": f"2026-11-0{rid}T06:00:00Z",
        "html_url": f"https://github.com/o/r/actions/runs/{rid}",
        "head_repository": {"full_name": head}, "head_sha": "abc", "name": "Nightly",
    }


def test_list_workflow_runs_builds_args():
    calls = []

    def fake_run(args):
        calls.append(args)
        return _ok({"workflow_runs": [_raw(3, "success"), _raw(2, "failure"), _raw(1, "success")]})

    runs = list_workflow_runs("o/r", "nightly.yml", branch="main", run=fake_run, limit=2)
    assert calls == [[
        "api",
        "repos/o/r/actions/workflows/nightly.yml/runs?branch=main&per_page=2&exclude_pull_requests=true",
    ]]
    assert runs == [
        {"id": 3, "status": "completed", "conclusion": "success", "event": "schedule",
         "updated_at": "2026-11-03T06:00:00Z", "html_url": "https://github.com/o/r/actions/runs/3"},
        {"id": 2, "status": "completed", "conclusion": "failure", "event": "schedule",
         "updated_at": "2026-11-02T06:00:00Z", "html_url": "https://github.com/o/r/actions/runs/2"},
    ]


def test_list_workflow_runs_quotes_branch_and_drops_foreign_head_runs():
    calls = []

    def fake_run(args):
        calls.append(args)
        return _ok({"workflow_runs": [_raw(2, "success", head="fork/r"), _raw(1, "success")]})

    runs = list_workflow_runs("o/r", "ci.yml", branch="release/1 x", run=fake_run)
    assert "branch=release%2F1%20x&per_page=30&" in calls[0][1]
    # A fork's run on a same-named branch is not this repo's code.
    assert [r["id"] for r in runs] == [1]


def test_list_workflow_runs_raises_on_gh_failure():
    def fake_run(args):
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")

    with pytest.raises(gitrepos.GhError):
        list_workflow_runs("o/r", "nope.yml", branch="main", run=fake_run)
