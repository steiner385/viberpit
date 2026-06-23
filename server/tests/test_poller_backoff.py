"""UsagePoller per-account adaptive backoff.

A persistently failing/throttled account (e.g. the usage endpoint 429ing for one
account) must not be re-fetched at full cadence on every sweep AND every dashboard
load — that is what hammered the kindash account into a 254-vs-17 429 asymmetry and
also blocked it from ever clearing back to 'ok'. After a failed fetch the account
enters an exponentially-growing cooldown during which get() serves the cached value
without issuing a network call. A success resets it; reset_backoff() clears it
immediately (e.g. right after a re-auth)."""
import asyncio

from server.usage.poller import UsagePoller


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_backoff_delay_grows_exponentially_and_caps():
    p = UsagePoller(poll_interval=60.0, backoff_base=60.0, backoff_cap=480.0,
                    jitter_frac=0.0)
    assert p._backoff_delay(1) == 60.0    # base * 2**0
    assert p._backoff_delay(2) == 120.0   # base * 2**1
    assert p._backoff_delay(3) == 240.0   # base * 2**2
    assert p._backoff_delay(4) == 480.0   # base * 2**3
    assert p._backoff_delay(5) == 480.0   # capped
    assert p._backoff_delay(99) == 480.0  # capped


def test_failed_fetch_cools_down_and_serves_cache_without_refetch():
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return None  # simulate a 429-throttled account (usage_for returns None)

    clock = FakeClock()

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0, backoff_base=60.0,
                        jitter_frac=0.0, clock=clock)
        await p.get("kindash")          # fetch #1 fails → cooldown 60s
        clock.advance(30)               # still inside cooldown
        await p.get("kindash")          # must be served from cache, no fetch
        return calls["n"]

    assert asyncio.run(run()) == 1


def test_cooldown_expires_then_refetches():
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return None

    clock = FakeClock()

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0, backoff_base=60.0,
                        jitter_frac=0.0, clock=clock)
        await p.get("kindash")          # fetch #1 → cooldown 60s
        clock.advance(61)               # cooldown elapsed
        await p.get("kindash")          # fetch #2 → cooldown grows to 120s
        return calls["n"]

    assert asyncio.run(run()) == 2


def test_success_does_not_cool_down():
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return f"ok-{account}"

    clock = FakeClock()

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0, backoff_base=60.0,
                        jitter_frac=0.0, clock=clock)
        await p.get("alpha")
        await p.get("alpha")            # no cooldown after success → fetches again
        return calls["n"]

    assert asyncio.run(run()) == 2


def test_success_after_failure_resets_backoff():
    results = [None, "recovered"]
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return results.pop(0) if results else "recovered"

    clock = FakeClock()

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0, backoff_base=60.0,
                        jitter_frac=0.0, clock=clock)
        await p.get("kindash")          # fail → cooldown
        clock.advance(61)
        await p.get("kindash")          # success → resets fail count + cooldown
        await p.get("kindash")          # immediately eligible again (no cooldown)
        return calls["n"]

    assert asyncio.run(run()) == 3


def test_reset_backoff_clears_cooldown_immediately():
    calls = {"n": 0}

    def usage_fn(account):
        calls["n"] += 1
        return None

    clock = FakeClock()

    async def run():
        p = UsagePoller(usage_fn=usage_fn, poll_interval=60.0, backoff_base=60.0,
                        jitter_frac=0.0, clock=clock)
        await p.get("kindash")          # fail → cooldown 60s
        p.reset_backoff("kindash")      # e.g. a fresh re-auth landed
        await p.get("kindash")          # eligible immediately despite no clock advance
        return calls["n"]

    assert asyncio.run(run()) == 2
