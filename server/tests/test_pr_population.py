import asyncio

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
