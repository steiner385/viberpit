"""actions_api — gh-backed reads, exercised through injected fake runners."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

from server.github.actions_api import (
    junit_artifacts, list_completed_runs, list_jobs, repo_ci_meta, workflow_texts,
)


def _ok(payload) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_list_completed_runs_normalizes_and_filters_cursor():
    payload = {"workflow_runs": [
        {"id": 5, "path": ".github/workflows/ci.yml", "event": "pull_request",
         "head_sha": "abc", "head_branch": "feat", "conclusion": "success",
         "run_started_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:05:00Z",
         "created_at": "2026-07-01T10:00:00Z"},
        {"id": 4, "path": ".github/workflows/ci.yml", "event": "push",
         "head_sha": "old", "head_branch": "master", "conclusion": "success",
         "run_started_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-01T10:04:00Z",
         "created_at": "2026-06-01T10:00:00Z"},
    ]}
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        return _ok(payload)
    from datetime import datetime, timezone
    cursor = datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp()
    runs = list_completed_runs("o/r", cursor, run=fake_run)
    assert [r["id"] for r in runs] == [5]           # cursor filters the June run
    assert runs[0]["duration_s"] == 300.0
    assert runs[0]["created_ts"] > cursor
    assert "actions/runs" in calls[0][1]


def test_list_jobs_durations():
    payload = {"jobs": [{"id": 9, "name": "e2e", "conclusion": "failure",
                         "started_at": "2026-07-01T10:00:00Z",
                         "completed_at": "2026-07-01T10:02:00Z"}]}
    (job,) = list_jobs("o/r", 5, run=lambda a, token=None: _ok(payload))
    assert job == {"id": 9, "name": "e2e", "conclusion": "failure", "duration_s": 120.0}


def test_junit_artifacts_unzips_xml_members_only():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("server.xml", "<testsuite/>")
        z.writestr("report.html", "<html/>")
    listing = {"artifacts": [
        {"id": 1, "name": "junit-python", "expired": False},
        {"id": 2, "name": "playwright-report", "expired": False},
    ]}
    out = junit_artifacts("o/r", 5, run=lambda a, token=None: _ok(listing),
                          run_bytes=lambda a: buf.getvalue())
    assert [(n, b) for n, b in out] == [("junit-python", b"<testsuite/>")]


def test_repo_ci_meta_error_degrades_to_empty():
    def fake_run(args, token=None):
        if "protection" in args[1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="404")
        return _ok({"default_branch": "master"})
    branch, required = repo_ci_meta("o/r", run=fake_run)
    assert branch == "master" and required == set()


def test_workflow_texts_decodes_contents():
    import base64
    listing = [{"path": ".github/workflows/ci.yml", "type": "file"}]
    content = {"content": base64.b64encode(b"name: CI\n").decode(), "encoding": "base64"}
    def fake_run(args, token=None):
        # Directory listing vs file read — both URLs end in `?ref=master`.
        is_listing = "contents/.github/workflows?" in args[1]
        return _ok(listing if is_listing else content)
    assert workflow_texts("o/r", "master", run=fake_run) == {
        ".github/workflows/ci.yml": "name: CI\n"}
