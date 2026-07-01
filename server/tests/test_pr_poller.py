import pytest

from server.github.pr_poller import PrStatusPoller
from server.github.pr_status import PullRequestInfo

PAIRS = [("o/r", "br")]


def _good_query(pairs):
    return {"data": {"r0": {"pullRequests": {"nodes": [{
        "number": 5, "url": "https://github.com/o/r/pull/5", "title": "x",
        "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-22T10:00:00Z",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }]}}}}


@pytest.mark.asyncio
async def test_refresh_populates_cache():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once(PAIRS) is True
    info = p.cached("o/r", "br")
    assert isinstance(info, PullRequestInfo) and info.number == 5 and info.state == "open"


@pytest.mark.asyncio
async def test_cached_none_for_unknown_or_null():
    p = PrStatusPoller(query_fn=_good_query)
    assert p.cached("o/r", "br") is None        # nothing fetched yet
    assert p.cached(None, "br") is None
    assert p.cached("o/r", None) is None


@pytest.mark.asyncio
async def test_refresh_failure_keeps_cache():
    p = PrStatusPoller(query_fn=_good_query)
    await p.refresh_once(PAIRS)

    def boom(pairs):
        raise RuntimeError("gh down")

    p._query_fn = boom
    assert await p.refresh_once(PAIRS) is False
    # Last-known value survives a failed refresh.
    assert p.cached("o/r", "br").number == 5


@pytest.mark.asyncio
async def test_refresh_empty_pairs_is_noop_success():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once([]) is True


@pytest.mark.asyncio
async def test_loop_accepts_async_pairs_provider():
    p = PrStatusPoller(query_fn=_good_query)
    async def async_pairs():
        return PAIRS
    p.set_pairs_provider(async_pairs)
    assert await p._resolve_pairs() == PAIRS

@pytest.mark.asyncio
async def test_resolve_pairs_handles_sync_provider():
    p = PrStatusPoller(query_fn=_good_query)
    p.set_pairs_provider(lambda: PAIRS)
    assert await p._resolve_pairs() == PAIRS


# ---------------------------------------------------------------------------
# Task 1 — on_change callback tests (M2b.2)
# ---------------------------------------------------------------------------

def _make_query(checks: str):
    """Return a query_fn that always returns a PR with the given checks value."""
    rollup = {"passing": "SUCCESS", "failing": "FAILURE", "pending": "PENDING"}.get(checks)
    rollup_node = {"state": rollup} if rollup else None

    def _query(pairs):
        return {"data": {"r0": {"pullRequests": {"nodes": [{
            "number": 1, "url": "https://github.com/o/r/pull/1", "title": "t",
            "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-24T10:00:00Z",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup_node}}]},
        }]}}}}

    return _query


@pytest.mark.asyncio
async def test_on_change_callback_fires_on_checks_transition():
    """Second refresh with changed checks calls on_change with (repo, branch, old, new)."""
    p = PrStatusPoller(query_fn=_make_query("passing"))
    calls = []

    def cb(repo, branch, old_checks, new_checks):
        calls.append((repo, branch, old_checks, new_checks))

    p.set_on_change(cb)

    # First refresh — cache was empty, old=None → should fire with old=None
    await p.refresh_once(PAIRS)
    assert len(calls) == 1
    assert calls[0] == ("o/r", "br", None, "passing")

    # Second refresh with same checks — no change → no call
    await p.refresh_once(PAIRS)
    assert len(calls) == 1  # still 1

    # Third refresh with different checks → fires
    p._query_fn = _make_query("failing")
    await p.refresh_once(PAIRS)
    assert len(calls) == 2
    assert calls[1] == ("o/r", "br", "passing", "failing")

    # Fourth refresh with same (failing) → no call
    await p.refresh_once(PAIRS)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_on_change_callback_exception_does_not_break_refresh():
    """A callback that raises must not prevent refresh_once from returning True."""
    p = PrStatusPoller(query_fn=_make_query("passing"))

    def boom(repo, branch, old_checks, new_checks):
        raise RuntimeError("callback exploded")

    p.set_on_change(boom)

    result = await p.refresh_once(PAIRS)
    assert result is True
    # Cache should still be populated despite the callback error
    info = p.cached("o/r", "br")
    assert info is not None and info.checks == "passing"


# ---------------------------------------------------------------------------
# on_cycle sweep hook — fires once per poll cycle AFTER refresh_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_on_cycle_stores_and_fire_invokes():
    """set_on_cycle stores the fn; _fire_on_cycle invokes it (sync + async)."""
    p = PrStatusPoller(query_fn=_good_query)
    calls = []
    p.set_on_cycle(lambda: calls.append("sync"))
    assert p._on_cycle is not None
    await p._fire_on_cycle()
    assert calls == ["sync"]

    async def async_cb():
        calls.append("async")

    p.set_on_cycle(async_cb)
    await p._fire_on_cycle()
    assert calls == ["sync", "async"]


@pytest.mark.asyncio
async def test_fire_on_cycle_noop_without_hook():
    """No registered hook → _fire_on_cycle is a harmless no-op."""
    p = PrStatusPoller(query_fn=_good_query)
    await p._fire_on_cycle()  # must not raise


@pytest.mark.asyncio
async def test_on_cycle_fires_after_refresh_and_reads_fresh_cache():
    """The sweep hook runs after refresh_once, so it sees the fresh cache."""
    p = PrStatusPoller(query_fn=_good_query)
    seen = []

    async def sweep():
        # Reads the cache the poller just refreshed.
        seen.append(p.cached("o/r", "br"))

    p.set_on_cycle(sweep)
    await p.refresh_once(PAIRS)
    await p._fire_on_cycle()
    assert len(seen) == 1
    assert seen[0] is not None and seen[0].number == 5


@pytest.mark.asyncio
async def test_on_cycle_exception_is_swallowed():
    """A raising sweep hook must not propagate (poll loop survives)."""
    p = PrStatusPoller(query_fn=_good_query)

    def boom():
        raise RuntimeError("sweep exploded")

    p.set_on_cycle(boom)
    await p._fire_on_cycle()  # must not raise

    async def aboom():
        raise RuntimeError("async sweep exploded")

    p.set_on_cycle(aboom)
    await p._fire_on_cycle()  # must not raise
