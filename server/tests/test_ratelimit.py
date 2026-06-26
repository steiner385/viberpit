import math

from server.api.ratelimit import TokenBucketRateLimiter


class _FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_allows_up_to_capacity_then_blocks():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=3, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None
    assert rl.check("a") is None
    assert rl.check("a") is None
    blocked = rl.check("a")
    assert blocked is not None and blocked > 0


def test_refills_over_time():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None          # consume the only token
    assert rl.check("a") is not None      # empty now
    clock.advance(1.0)                    # one token refilled
    assert rl.check("a") is None


def test_retry_after_reflects_deficit():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=0.5, now=clock)
    assert rl.check("a") is None
    retry = rl.check("a")
    # need 1 token at 0.5/sec => ~2s
    assert retry is not None and math.isclose(retry, 2.0, rel_tol=0.05)


def test_distinct_keys_are_independent():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None
    assert rl.check("b") is None          # b unaffected by a
    assert rl.check("a") is not None      # a still exhausted


def test_does_not_exceed_capacity_on_long_idle():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=2, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None          # consume 1 from an existing bucket -> 1 left
    clock.advance(1000.0)                 # would refill 1000 tokens; must clamp to capacity 2
    assert rl.check("a") is None
    assert rl.check("a") is None
    assert rl.check("a") is not None      # only 2 available after idle, not 1001


def test_prune_drops_idle_full_buckets_over_max_keys():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock, max_keys=2)
    rl.check("a")                         # a now empty (active)
    clock.advance(10.0)                   # a refills to full (idle)
    rl.check("b")                         # b active/empty
    rl.check("c")                         # exceeds max_keys=2 -> prune full/idle buckets (a)
    assert "a" not in rl._buckets
    # active buckets survive
    assert "b" in rl._buckets and "c" in rl._buckets
