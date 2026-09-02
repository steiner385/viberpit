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
    # #451: the `>=` qualifier is percent-encoded, never sent raw.
    assert "created=%3E%3D" in calls[0][1]
    assert "created=>=" not in calls[0][1]


def _run_payload(n, start_id=0):
    return {"workflow_runs": [
        {"id": start_id + i, "path": ".github/workflows/ci.yml", "event": "push",
         "head_sha": f"s{i}", "head_branch": "master", "conclusion": "success",
         "run_started_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:01:00Z",
         "created_at": "2026-07-01T10:00:00Z"} for i in range(n)]}


def test_list_completed_runs_paginates_full_pages():
    """#451: a full 100-run page triggers the next page; a short page stops it."""
    pages = {1: _run_payload(100, start_id=0), 2: _run_payload(30, start_id=100)}
    calls = []
    def fake_run(args, token=None):
        calls.append(args[1])
        page = int(args[1].split("&page=")[1].split("&")[0])
        return _ok(pages.get(page, {"workflow_runs": []}))
    runs = list_completed_runs("o/r", None, run=fake_run)
    assert len(runs) == 130
    assert len(calls) == 2  # page 1 (full) then page 2 (short → stop)


def test_list_completed_runs_backfill_is_bounded():
    """A repo with endless history is capped at max_pages, never unbounded."""
    calls = []
    def fake_run(args, token=None):
        calls.append(args[1])
        return _ok(_run_payload(100))  # always a full page
    runs = list_completed_runs("o/r", None, run=fake_run, max_pages=3)
    assert len(calls) == 3          # stopped at the cap, not looping forever
    assert len(runs) == 300


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


def test_default_runner_authenticates_as_owner_app(monkeypatch):
    """With no run= injected, the doctor's default runner threads the repo owner's
    App installation token (issue #913) — not the ambient gh user, whose expired
    credential drops gh to anonymous and 404s private repos."""
    from ccswitch import gitrepos
    monkeypatch.setattr(gitrepos, "_app_token", lambda owner: f"ghs_{owner}")
    seen = {}

    def fake_run_gh(args, token=None, *, input=None, timeout=None):
        seen["token"] = token
        return _ok({"jobs": []})

    monkeypatch.setattr(gitrepos, "_run_gh", fake_run_gh)
    list_jobs("acme/widget", 7)  # no run= → owner-bound default runner
    assert seen["token"] == "ghs_acme"


def test_default_run_bytes_authenticates_as_owner_app(monkeypatch):
    """The artifact-zip fetch (_run_gh_bytes) is bound to the owner's App token too."""
    from ccswitch import gitrepos
    from server.github import actions_api
    monkeypatch.setattr(gitrepos, "_app_token", lambda owner: f"ghs_{owner}")
    seen = {}

    def fake_run_bytes(args, token=None):
        seen["token"] = token
        return b""

    monkeypatch.setattr(actions_api, "_run_gh_bytes", fake_run_bytes)
    # No artifacts to fetch, so run= (the listing) can be a trivial stub; run_bytes is
    # only exercised when an artifact matches, so drive it directly via the binder.
    actions_api._run_gh_bytes_for("acme/widget")(["api", "x"])
    assert seen["token"] == "ghs_acme"


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
