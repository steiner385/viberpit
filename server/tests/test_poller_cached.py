"""UsagePoller.cached(): read last-known usage without triggering a fetch."""
import asyncio

from server.usage.poller import UsagePoller


def test_cached_returns_none_before_any_fetch():
    poller = UsagePoller(usage_fn=lambda a: None)
    assert poller.cached("acct1") is None


def test_cached_returns_last_fetched_value_without_refetching():
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return f"usage-for-{account}"

    async def run():
        poller = UsagePoller(usage_fn=usage_fn)
        await poller.get("acct1")            # one fetch populates the cache
        before = calls["n"]
        value = poller.cached("acct1")       # read-only — must not fetch again
        return value, before, calls["n"]

    value, before, after = asyncio.run(run())
    assert value == "usage-for-acct1"
    assert after == before  # cached() did not call usage_fn
