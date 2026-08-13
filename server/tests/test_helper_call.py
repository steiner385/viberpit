"""Tests for the shared helper-call backoff + circuit breaker."""
import asyncio
from types import SimpleNamespace

import pytest

from server.sessions import helper_call
from server.sessions.helper_call import run_helper_call


@pytest.fixture(autouse=True)
def _clean_breakers():
    helper_call.reset_breakers()
    yield
    helper_call.reset_breakers()


class Clock:
    """Manually-advanced monotonic clock for deterministic breaker tests."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _sleeps_recorded():
    """A fake _sleep that records delays instead of blocking."""
    delays = []

    async def _sleep(d):
        delays.append(d)

    return delays, _sleep


def _assistant(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _result(api_error_status):
    return SimpleNamespace(api_error_status=api_error_status, is_error=True, subtype="success")


def _stream_factory(*, yield_msgs=(), raise_exc=None, counter=None):
    """Return a make_stream() producing a fresh async iterator each call."""
    def make_stream():
        if counter is not None:
            counter["n"] += 1

        async def gen():
            for m in yield_msgs:
                yield m
            if raise_exc is not None:
                raise raise_exc
        return gen()
    return make_stream


def _run(make_stream, **kw):
    kw.setdefault("subsystem", "test")
    kw.setdefault("account_key", "/cfg/acct1")
    return asyncio.run(run_helper_call(make_stream, **kw))


def test_success_returns_messages_and_keeps_lane_closed():
    out = _run(_stream_factory(yield_msgs=[_assistant("hi")]))
    assert out.error is None and out.skipped is False
    assert [getattr(m.content[0], "text") for m in out.messages] == ["hi"]


def test_rate_limit_is_not_retried_and_opens_lane_immediately():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    # SDK shape for a 429: yields a ResultMessage with api_error_status, then raises.
    ms = _stream_factory(
        yield_msgs=[_assistant("Rate limit exceeded. API Error: 429"), _result(429)],
        raise_exc=RuntimeError("Claude Code returned an error result: success"),
        counter=counter,
    )
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.error is not None
    assert out.failure_kind == "rate_limit"
    assert out.api_error_status == 429
    assert counter["n"] == 1          # NOT retried
    assert delays == []               # no backoff sleep
    # Lane opened immediately — a second call is skipped without invoking the stream.
    counter["n"] = 0
    out2 = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                       _now=clock, _sleep=sleep))
    assert out2.skipped is True
    assert counter["n"] == 0


def test_transient_overload_is_retried_then_fails():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    ms = _stream_factory(raise_exc=RuntimeError("API Error: 503 overloaded"), counter=counter)
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.failure_kind == "overloaded"
    assert counter["n"] == 4          # initial + 3 retries
    assert delays == [1.0, 2.0, 4.0]  # exponential backoff


def test_circuit_opens_after_five_consecutive_other_failures():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    ms = _stream_factory(raise_exc=RuntimeError("boom"))  # classifies as "other", not retried
    for _ in range(5):
        out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                          _now=clock, _sleep=sleep))
        assert out.skipped is False
    # Sixth call: lane is open, skipped.
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is True


def test_half_open_trial_after_cooldown_closes_on_success():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    fail = _stream_factory(raise_exc=RuntimeError("Rate limit 429"))
    # One 429 opens the lane immediately.
    asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                _now=clock, _sleep=sleep))
    assert asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                       _now=clock, _sleep=sleep)).skipped is True
    # After the cooldown, a half-open trial is allowed and its success closes the lane.
    clock.advance(61)
    ok = _stream_factory(yield_msgs=[_assistant("recovered")])
    out = asyncio.run(run_helper_call(ok, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is False and out.error is None


def test_lanes_are_isolated_per_account_and_subsystem():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    fail = _stream_factory(raise_exc=RuntimeError("429 rate limit"))
    asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/bad",
                                _now=clock, _sleep=sleep))
    # A different account's lane is unaffected.
    ok = _stream_factory(yield_msgs=[_assistant("fine")])
    out = asyncio.run(run_helper_call(ok, subsystem="test", account_key="/cfg/good",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is False and out.error is None
    # Same account, different subsystem is also its own lane.
    out2 = asyncio.run(run_helper_call(ok, subsystem="other", account_key="/cfg/bad",
                                       _now=clock, _sleep=sleep))
    assert out2.skipped is False


def test_per_attempt_timeout_fails_open_without_retry():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}

    def make_stream():
        counter["n"] += 1

        async def gen():
            await asyncio.sleep(10)
            yield _assistant("never")
        return gen()

    out = asyncio.run(run_helper_call(make_stream, subsystem="test", account_key="/cfg/acct1",
                                      per_attempt_timeout=0.01, _now=clock, _sleep=sleep))
    assert out.error is not None
    assert out.failure_kind == "timeout"
    assert counter["n"] == 1   # a wedged call is not retried
    assert delays == []
