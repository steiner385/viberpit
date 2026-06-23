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
