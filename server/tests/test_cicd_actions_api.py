"""actions_api — gh-backed reads, exercised through injected fake runners."""
from __future__ import annotations

import io
import json
import logging
import zipfile
from types import SimpleNamespace

from viberpit import gitrepos
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
         "head_repository": {"full_name": "o/r"},
         "run_started_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:05:00Z",
         "created_at": "2026-07-01T10:00:00Z"},
        {"id": 4, "path": ".github/workflows/ci.yml", "event": "push",
         "head_sha": "old", "head_branch": "master", "conclusion": "success",
         "head_repository": {"full_name": "o/r"},
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
         "head_repository": {"full_name": "o/r"},
         "run_started_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:01:00Z",
         "created_at": "2026-07-01T10:00:00Z"} for i in range(n)]}


def test_list_completed_runs_drops_runs_of_fork_code():
    """A fork PR's run executes the FORK's code, so its test ids, failure messages and
    junit artifacts are attacker-written. CI/CD Doctor files issues from them under the
    repo's own identity -- which the trusted-author gate trusts -- so those runs must
    never be ingested. Only runs whose head repository IS this repo (case-insensitive)
    are kept; a run with no head repository (e.g. a deleted fork) is dropped too."""
    base = _run_payload(4)["workflow_runs"]
    base[1]["head_repository"] = {"full_name": "stranger/r"}     # fork PR
    base[2]["head_repository"] = None                            # deleted fork
    base[3]["head_repository"] = {"full_name": "O/R"}            # same repo, other case
    runs = list_completed_runs("o/r", None, run=lambda args, token=None: _ok(
        {"workflow_runs": base}))
    assert [r["id"] for r in runs] == [0, 3]


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


def _junit_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("server.xml", "<testsuite/>")
    return buf.getvalue()


_JUNIT_LISTING = {"artifacts": [{"id": 1, "name": "junit-python-server-shard-4",
                                 "expired": False}]}


def test_junit_artifacts_retries_github_5xx_then_succeeds():
    """A GitHub-side 5xx on the artifact zip is transient (#1839): retry it rather
    than dropping that shard's results from the run for good."""
    calls, slept = [], []

    def flaky(args):
        calls.append(args)
        if len(calls) < 3:
            raise gitrepos.GhError("gh: HTTP 503")
        return _junit_zip()

    out = junit_artifacts("o/r", 5, run=lambda a, token=None: _ok(_JUNIT_LISTING),
                          run_bytes=flaky, sleep=slept.append)
    assert out == [("junit-python-server-shard-4", b"<testsuite/>")]
    assert len(calls) == 3 and len(slept) == 2


def test_junit_artifacts_exhausted_5xx_logs_info_not_warning(caplog):
    """A 5xx that survives every retry is GitHub's outage, not a ViberPit defect:
    INFO, so error triage does not file it as an auto-error issue (#1839)."""

    def down(args):
        raise gitrepos.GhError("gh: HTTP 503")

    with caplog.at_level(logging.DEBUG):
        out = junit_artifacts("o/r", 5, run=lambda a, token=None: _ok(_JUNIT_LISTING),
                              run_bytes=down, sleep=lambda s: None)
    assert out == []
    hits = [r for r in caplog.records if "artifact_fetch" in r.getMessage()]
    assert hits and all(r.levelno < logging.WARNING for r in hits)


def test_junit_artifacts_non_5xx_failure_still_warns_without_retry(caplog):
    """Only a 5xx is retried and demoted; a 404 or corrupt zip is still a WARNING."""
    calls = []

    def missing(args):
        calls.append(args)
        raise gitrepos.GhError("gh: Not Found (HTTP 404)")

    with caplog.at_level(logging.DEBUG):
        junit_artifacts("o/r", 5, run=lambda a, token=None: _ok(_JUNIT_LISTING),
                        run_bytes=missing, sleep=lambda s: None)
    assert len(calls) == 1
    assert any(r.levelno == logging.WARNING and "artifact_fetch_failed" in r.getMessage()
               for r in caplog.records)


# ---- systemic-red seams ----

def _recent_runner(calls, runs, jobs_by_run):
    def run(args, token=None):
        calls.append(args)
        url = args[1]
        if "/jobs" in url:
            rid = int(url.split("/runs/")[1].split("/")[0])
            return _ok({"jobs": jobs_by_run[rid]})
        return _ok({"workflow_runs": runs})
    return run


def _wf_run(rid, status, attempt=1):
    return {"id": rid, "workflow_id": 9, "event": "pull_request", "head_branch": "f",
            "head_sha": "s", "created_at": "2026-10-31T10:00:00Z", "status": status,
            "conclusion": "failure" if status == "completed" else None,
            "run_attempt": attempt,
            "head_repository": {"full_name": "O/R"}}


_JOB = {"id": 70, "name": "ci", "status": "completed", "conclusion": "failure",
        "completed_at": "2026-10-31T10:05:00Z"}


def test_list_recent_runs_always_bounds_created_and_includes_in_progress():
    from server.github.actions_api import list_recent_runs
    calls: list = []
    run = _recent_runner(calls, [_wf_run(1, "in_progress")], {1: [_JOB]})
    out = list_recent_runs("o/r", event="pull_request", since_ts=1_790_000_000.0, run=run)
    first = calls[0][1]
    assert "created=%3E%3D2026-" in first
    assert "status=completed" not in first
    assert "event=pull_request" in first
    assert out[0].status == "in_progress" and out[0].jobs[0].job_id == 70
    assert out[0].jobs[0].completed_ts is not None


def test_completed_run_jobs_fetched_once_in_progress_refetched():
    from server.github import actions_api
    actions_api._JOB_CACHE.clear()
    calls: list = []
    run = _recent_runner(calls, [_wf_run(1, "completed"), _wf_run(2, "in_progress")],
                         {1: [_JOB], 2: [_JOB]})
    for _ in range(2):
        actions_api.list_recent_runs("o/r", event="pull_request", since_ts=1_790_000_000.0,
                                     run=run)
    jobs_calls = [c[1] for c in calls if "/jobs" in c[1]]
    assert sum("/runs/1/" in u for u in jobs_calls) == 1
    assert sum("/runs/2/" in u for u in jobs_calls) == 2
    actions_api._JOB_CACHE.clear()


def test_job_log_text_returns_empty_on_failure_and_tails_ok():
    from server.github.actions_api import job_log_text

    def boom(args):
        raise gitrepos.GhError("nope")
    assert job_log_text("o/r", 5, run=boom) == ""
    big = b"x" * (3 * 1024 * 1024) + b"END"
    got = job_log_text("o/r", 5, run=lambda args: big)
    assert len(got) == 2 * 1024 * 1024 and got.endswith("END")


def test_list_recent_runs_skips_fork_runs():
    from server.github import actions_api
    actions_api._JOB_CACHE.clear()
    fork = {**_wf_run(2, "completed"), "head_repository": {"full_name": "evil/r"}}
    gone = {**_wf_run(3, "completed"), "head_repository": None}
    calls: list = []
    run = _recent_runner(calls, [_wf_run(1, "completed"), fork, gone], {1: [_JOB]})
    out = actions_api.list_recent_runs("o/r", event="pull_request", since_ts=1_790_000_000.0,
                                       run=run)
    assert [r.run_id for r in out] == [1]
    assert not any("/runs/2/" in c[1] or "/runs/3/" in c[1] for c in calls)
    actions_api._JOB_CACHE.clear()
