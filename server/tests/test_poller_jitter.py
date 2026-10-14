"""UsagePoller inter-sweep jitter: spread sweeps so N pollers/accounts don't
resync into a lockstep burst that self-triggers 429s."""
import pytest

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


async def _run_loop_once(obj, sweep_attr, monkeypatch, logger_name, caplog):
    """Run ``obj._loop`` through one failing sweep, stopping at the inter-sweep sleep."""
    import asyncio
    import logging

    async def boom():
        raise OSError(17, "File exists")

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(obj, sweep_attr, boom)
    monkeypatch.setattr(asyncio, "sleep", stop)
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        try:
            await obj._loop()
        except asyncio.CancelledError:
            pass
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_poller_loop_logs_oserror_below_warning(monkeypatch, caplog):
    """#3057: a mount blip must not file an auto-error."""
    p = UsagePoller(poll_interval=60.0)
    assert not await _run_loop_once(p, "all", monkeypatch, "server.usage.poller", caplog)
    assert any("retrying next sweep" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_process_quota_loop_logs_oserror_below_warning(monkeypatch, caplog):
    from server.tests.test_process_quota import FakeSource, _refresher
    r = _refresher(FakeSource("copilot"), list_accounts=lambda: [])
    assert not await _run_loop_once(
        r, "sweep", monkeypatch, "server.usage.process_quota", caplog)
