"""UsagePoller inter-sweep jitter: spread sweeps so N pollers/accounts don't
resync into a lockstep burst that self-triggers 429s."""
from server.usage.poller import UsagePoller


def test_next_delay_is_plain_interval_with_zero_jitter():
    p = UsagePoller(poll_interval=60.0, jitter_frac=0.0, rng=lambda: 1.0)
    assert p._next_delay() == 60.0


def test_next_delay_adds_full_jitter_at_rng_max():
    p = UsagePoller(poll_interval=60.0, jitter_frac=0.1, rng=lambda: 1.0)
    assert p._next_delay() == 66.0  # interval * (1 + 0.1 * 1.0)


def test_next_delay_adds_no_jitter_at_rng_min():
    p = UsagePoller(poll_interval=60.0, jitter_frac=0.1, rng=lambda: 0.0)
    assert p._next_delay() == 60.0


def test_next_delay_stays_within_bounds():
    p = UsagePoller(poll_interval=60.0, jitter_frac=0.25, rng=lambda: 0.5)
    d = p._next_delay()
    assert 60.0 <= d <= 75.0  # never below interval, never above interval*(1+frac)
