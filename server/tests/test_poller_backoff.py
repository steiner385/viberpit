"""UsagePoller delegates backoff to usage_fn — it must not gate fetches itself.

The fetch-failure backoff is owned by ccswitch.accounts.usage_for via the
ledger (usageFetchFails/usageFetchCooldownUntil, PR #539): one cooldown per
account shared by every caller in every process. The poller keeping its OWN
in-memory cooldown was a parallel implementation of the same concept, and it
masked recoveries: after a re-auth cleared the ledger backoff and fresh usage
landed in the ledger, the poller kept serving its cached None until its
private cooldown expired (kindash, 2026-07-09). get() must always delegate;
usage_for makes that cheap (fresh cache → instant hit, cooldown → instant
None). The poller still counts consecutive failures — the rotation monitor
uses that to spot persistently dead accounts.
"""
import asyncio

from server.usage.poller import UsagePoller


def test_get_always_delegates_no_in_memory_cooldown():
    """A failed fetch must not stop the next get() from calling usage_fn.

    usage_for's ledger cooldown decides whether real network I/O happens; if
    the poller also gated, a recovery visible in the ledger (re-auth, CLI
    fetch, selection probe) stayed masked behind the poller's cached None.
    """
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return None  # throttled/failing account: usage_for returns None

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0)
        await p.get("kindash")
        await p.get("kindash")
        return calls["n"]

    assert asyncio.run(run()) == 2


def test_consecutive_failures_counts_and_success_resets():
    results = iter([None, None, {"ok": True}])

    def usage_fn(account):
        return next(results)

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0)
        await p.get("a")
        await p.get("a")
        assert p.consecutive_failures("a") == 2
        await p.get("a")
        assert p.consecutive_failures("a") == 0

    asyncio.run(run())


def test_failure_then_recovery_is_served_immediately():
    """The moment usage_fn can return data (e.g. ledger refreshed after a
    re-auth), get() returns it — no cooldown tail."""
    results = iter([None, {"session": 12.0}])

    def usage_fn(account):
        return next(results)

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0)
        assert await p.get("a") is None
        assert await p.get("a") == {"session": 12.0}

    asyncio.run(run())
