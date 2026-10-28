import asyncio
import logging
import subprocess

import pytest

import server.github.pr_population as pop
from server.github.pr_population import PrPopulationPoller


def _resp(number: int, state: str = "OPEN"):
    return {"data": {"repo": {
        "deployments": {"nodes": []},
        "pullRequests": {"nodes": [{
            "number": number, "url": f"u{number}", "title": "t", "state": state,
            "isDraft": False, "updatedAt": "2026-06-25T00:00:00Z", "mergedAt": None,
            "headRefOid": "x", "mergeStateStatus": "CLEAN",
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                {"state": "SUCCESS", "contexts": {"nodes": []}}}}]},
        }]},
    }, "merged": {"nodes": []}}}


def test_refresh_once_caches_per_repo():
    calls = []

    def qfn(repo: str) -> dict:
        calls.append(repo)
        return _resp(1 if repo == "o/a" else 2)

    p = PrPopulationPoller(query_fn=qfn)
    p.set_repos_provider(lambda: ["o/a", "o/b"])
    asyncio.run(p.refresh_once(["o/a", "o/b"]))

    assert sorted(calls) == ["o/a", "o/b"]
    assert [pr.number for pr in p.cached("o/a")] == [1]
    assert [pr.number for pr in p.cached("o/b")] == [2]
    assert p.cached("o/a")[0].repo == "o/a"


def test_one_failing_repo_does_not_blank_others():
    def qfn(repo: str) -> dict:
        if repo == "o/bad":
            raise RuntimeError("gh exploded")
        return _resp(5)

    p = PrPopulationPoller(query_fn=qfn)
    asyncio.run(p.refresh_once(["o/good", "o/bad"]))

    assert [pr.number for pr in p.cached("o/good")] == [5]
    assert p.cached("o/bad") == ()  # failed repo simply has no entry; good one survives


def test_cached_unknown_repo_is_empty_tuple():
    p = PrPopulationPoller(query_fn=lambda r: _resp(1))
    assert p.cached("never/seen") == ()


def test_failed_repo_records_reason_and_success_clears_it():
    """A repo whose fetch fails (e.g. GitHub 504) has no cache entry to fall back on,
    so without this it just vanished from all()/cached() with only a WARNING line —
    cairnea/KinDash sat invisible on the pipeline board for exactly that reason."""
    failing = {"o/bad"}

    def qfn(repo: str) -> dict:
        if repo in failing:
            raise RuntimeError("gh: HTTP 504")
        return _resp(5)

    p = PrPopulationPoller(query_fn=qfn)
    asyncio.run(p.refresh_once(["o/good", "o/bad"]))

    assert p.last_error("o/bad") == "gh: HTTP 504"
    assert p.errors() == {"o/bad": "gh: HTTP 504"}
    assert p.last_error("o/good") is None
    assert p.cached("o/bad") == ()  # still no entry — the reason is what's new

    failing.clear()  # GitHub recovers
    asyncio.run(p.refresh_once(["o/good", "o/bad"]))
    assert p.last_error("o/bad") is None
    assert p.errors() == {}
    assert [pr.number for pr in p.cached("o/bad")] == [5]


def test_error_entry_dropped_when_repo_no_longer_watched():
    def qfn(repo: str) -> dict:
        raise RuntimeError("boom")

    p = PrPopulationPoller(query_fn=qfn)
    asyncio.run(p.refresh_once(["o/gone"]))
    assert "o/gone" in p.errors()
    asyncio.run(p.refresh_once(["o/other"]))
    assert "o/gone" not in p.errors()


def test_last_error_unknown_or_empty_repo_is_none():
    p = PrPopulationPoller(query_fn=lambda r: _resp(1))
    assert p.last_error("never/seen") is None
    assert p.last_error(None) is None


def test_github_5xx_logs_info_not_a_warning(caplog):
    """A GitHub-side 5xx is the poller's own outage to wait out, not a defect to
    chase (#1839's read). This loop retries every tick, so filing it at WARNING
    the old way re-mints the same non-actionable auto-error issue on a schedule
    — #1681 was 600+ occurrences of exactly that in a day."""
    def qfn(repo: str) -> dict:
        raise pop.gitrepos.GhError("gh: HTTP 502")

    p = PrPopulationPoller(query_fn=qfn)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(p.refresh_once(["o/bad"]))

    assert p.cached("o/bad") == ()
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [], f"a 5xx must not warn: {[r.getMessage() for r in warned]}"
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("pr_population.refresh_unavailable" in r.getMessage() for r in info)


def test_graphql_internal_error_logs_info_not_a_warning(caplog):
    """#3025: GitHub's generic GraphQL internal-error envelope is the same
    transient as a 5xx; its per-occurrence request id must not file issues."""
    def qfn(repo: str) -> dict:
        raise pop.gitrepos.GhError(
            "gh: Something went wrong while executing your query on "
            "2026-10-12T12:52:08Z. Please include `8A50:3B861C:7E1F7:1A4AE1:6ACCD7F6`"
            " when reporting this issue.")

    p = PrPopulationPoller(query_fn=qfn)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(p.refresh_once(["o/bad"]))

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [], [r.getMessage() for r in warned]
    assert any("pr_population.refresh_unavailable" in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO)


def test_non_5xx_failure_still_warns(caplog):
    """The demotion is specific to a GitHub 5xx — any other target-specific
    failure keeps reporting at WARNING with repo= exactly as before."""
    def qfn(repo: str) -> dict:
        raise pop.gitrepos.GhError("gh: not found (HTTP 404)")

    p = PrPopulationPoller(query_fn=qfn)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(p.refresh_once(["o/bad"]))

    assert p.cached("o/bad") == ()
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert "pr_population.refresh_failed repo=o/bad" in warned[0].getMessage()


@pytest.mark.parametrize("stderr", [
    "unexpected end of JSON input",                            # #2490
    "stream error: stream ID 1; CANCEL; received from peer",   # #2677
])
def test_fetch_merged_edge_cutoff_logs_info_not_a_warning(caplog, monkeypatch, stderr):
    """#2490/#2677: GitHub's edge can cut the merged-half response short —
    surfacing as gh's Go JSON decoder hitting "unexpected end of JSON input", or
    its HTTP/2 client reporting a peer stream reset — rather than a
    ``gh: ... (HTTP 5xx)``-style message. The same self-healing edge condition
    as a bare 5xx (#1839's read), just a different gh error string. This loop
    retries every poll, so it must not also file an auto-error."""
    def fake_run_gh(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(pop.gitrepos, "_run_gh", fake_run_gh)

    with caplog.at_level(logging.DEBUG):
        merged = pop._fetch_merged("o/bad", 14, token=None)

    assert merged is None
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [], f"a truncated response must not warn: {[r.getMessage() for r in warned]}"
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("pr_population.merged_fetch_unavailable" in r.getMessage() for r in info)


def test_fetch_merged_non_transient_failure_still_warns(caplog, monkeypatch):
    """The demotion is specific to a transient GitHub-edge failure — any other
    target-specific failure keeps reporting at WARNING with repo= as before."""
    def fake_run_gh(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "gh: not found (HTTP 404)")

    monkeypatch.setattr(pop.gitrepos, "_run_gh", fake_run_gh)

    with caplog.at_level(logging.DEBUG):
        merged = pop._fetch_merged("o/bad", 14, token=None)

    assert merged is None
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert "pr_population.merged_fetch_failed repo=o/bad" in warned[0].getMessage()


@pytest.mark.parametrize("exc,level", [
    (ConnectionRefusedError(111, "Connection refused"), logging.INFO),
    (ValueError("bad sql"), logging.WARNING),
])
def test_timeline_capture_db_failure_log_level(monkeypatch, caplog, exc, level):
    """A DB connectivity blip is INFO (no auto-error); a real defect stays WARNING (#2792)."""
    async def boom(*_a, **_k):
        raise exc

    p = PrPopulationPoller(query_fn=lambda repo: _resp(1))
    monkeypatch.setattr(p, "_capture_timeline", boom)
    with caplog.at_level(logging.INFO, logger=pop.log.name):
        asyncio.run(p.refresh_once(["o/a"]))

    recs = [r for r in caplog.records if "pr_population.timeline_capture_failed" in r.getMessage()]
    assert [r.levelno for r in recs] == [level]


def _env_supplement_response():
    return {"data": {"repo": {
        "deployments": {"nodes": []},
        "environments": {"nodes": [{"name": "admin-production"}]},
    }}}


@pytest.mark.parametrize("stderr,warns", [
    ("gh: HTTP 502", False),                  # #3155: GitHub edge, retried next tick
    ("gh: not found (HTTP 404)", True),
])
def test_env_supplement_edge_failure_demotes_to_info(caplog, monkeypatch, stderr, warns):
    def fake_run_gh(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(pop.gitrepos, "_run_gh", fake_run_gh)

    with caplog.at_level(logging.DEBUG):
        pop._supplement_env_deployments(_env_supplement_response(), "o/r", None)

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert bool(warned) is warns
    assert any(
        "pr_population.env_supplement_failed" in r.getMessage() for r in caplog.records
    )
