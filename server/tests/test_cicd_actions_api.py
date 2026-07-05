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


def test_list_completed_runs_normalizes_and_3day_lookback():
    """list_completed_runs applies a 3-day server-side lookback and returns all
    runs the server returns (no strict client-side created <= since_ts filter).
    Idempotence lives in _ingest's seen set, not here."""
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
    # Both runs are returned — no client-side filtering; server uses 3-day lookback date.
    assert [r["id"] for r in runs] == [4, 5]         # sorted by created_ts
    assert runs[1]["duration_s"] == 300.0
    assert "actions/runs" in calls[0][1]
    # The date qualifier uses since_ts - 3*86400, not since_ts itself.
    lookback = datetime(2026, 6, 12, tzinfo=timezone.utc).strftime("%Y-%m-%d")
    assert lookback in calls[0][1]


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


def _workflow_fake(sha="sha1", body=b"name: CI\n"):
    """A gh runner that answers the three workflow_texts endpoints and records
    how many contents/commits calls it served."""
    import base64
    listing = [{"path": ".github/workflows/ci.yml", "type": "file"}]
    content = {"content": base64.b64encode(body).decode(), "encoding": "base64"}
    calls = {"commits": 0, "contents": 0, "sha": sha}
    def fake_run(args, token=None):
        url = args[1]
        if url.startswith("repos/o/r/commits/"):
            calls["commits"] += 1
            return _ok({"sha": calls["sha"]})
        calls["contents"] += 1
        is_listing = "contents/.github/workflows?" in url
        return _ok(listing if is_listing else content)
    return fake_run, calls


def test_workflow_texts_decodes_contents():
    fake_run, _ = _workflow_fake()
    assert workflow_texts("o/r", "master", run=fake_run, cache={}) == {
        ".github/workflows/ci.yml": "name: CI\n"}


def test_workflow_texts_caches_by_head_sha():
    """#452: an unchanged default-branch HEAD serves from cache — no contents reads."""
    fake_run, calls = _workflow_fake(sha="sha1")
    cache: dict = {}
    first = workflow_texts("o/r", "master", run=fake_run, cache=cache)
    contents_after_first = calls["contents"]
    assert contents_after_first > 0  # the first tick did the listing + file reads
    second = workflow_texts("o/r", "master", run=fake_run, cache=cache)
    assert second == first
    assert calls["contents"] == contents_after_first  # cache hit: no new contents reads
    assert calls["commits"] == 2                       # only the cheap sha check re-ran


def test_workflow_texts_refetches_when_head_sha_changes():
    """A new commit on the default branch invalidates the cache."""
    fake_run, calls = _workflow_fake(sha="sha1")
    cache: dict = {}
    workflow_texts("o/r", "master", run=fake_run, cache=cache)
    before = calls["contents"]
    calls["sha"] = "sha2"  # a commit landed
    workflow_texts("o/r", "master", run=fake_run, cache=cache)
    assert calls["contents"] > before  # refetched


def test_workflow_texts_listing_failure_not_cached():
    """A transient listing failure must not poison the cache under that sha."""
    import base64
    content = {"content": base64.b64encode(b"name: CI\n").decode()}
    listing = [{"path": ".github/workflows/ci.yml", "type": "file"}]
    state = {"fail_listing": True}
    def fake_run(args, token=None):
        url = args[1]
        if url.startswith("repos/o/r/commits/"):
            return _ok({"sha": "sha1"})
        if "contents/.github/workflows?" in url:
            if state["fail_listing"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="502")
            return _ok(listing)
        return _ok(content)
    cache: dict = {}
    assert workflow_texts("o/r", "master", run=fake_run, cache=cache) == {}
    assert "o/r" not in cache          # not cached under sha1
    state["fail_listing"] = False
    assert workflow_texts("o/r", "master", run=fake_run, cache=cache) == {
        ".github/workflows/ci.yml": "name: CI\n"}  # retried, now succeeds
