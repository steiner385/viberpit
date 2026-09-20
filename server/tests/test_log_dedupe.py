"""The windowed log throttle behind #1702 / #1852.

Pure unit tests over an injected clock — the module exists precisely so nothing
has to sleep to prove that a repeat was suppressed.
"""
from __future__ import annotations

import logging
import math

import pytest

from server.obs.logdedupe import LogThrottle, log_throttled


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_first_sighting_reports_and_an_immediate_repeat_does_not():
    clock = _Clock()
    t = LogThrottle(window_s=100.0, clock=clock)

    assert t.should_log("k") is True
    assert t.should_log("k") is False
    assert t.should_log("k") is False


def test_a_different_key_is_never_suppressed_by_another_keys_window():
    clock = _Clock()
    t = LogThrottle(window_s=100.0, clock=clock)

    assert t.should_log("a") is True
    assert t.should_log("b") is True, "b was silenced by a's window"


def test_the_window_reopens_so_a_persistent_failure_keeps_a_heartbeat():
    """The reason this is a window and not a one-shot latch: an operator reading
    the journal must be able to tell 'still broken' from 'stopped happening'."""
    clock = _Clock()
    t = LogThrottle(window_s=100.0, clock=clock)

    assert t.should_log("k") is True
    clock.t = 99.0
    assert t.should_log("k") is False
    clock.t = 100.0
    assert t.should_log("k") is True


def test_the_window_runs_from_the_last_reported_occurrence_not_the_last_attempt():
    """A suppressed repeat must not push the next report further out, or a loop
    that ticks faster than the window would hold the record down forever."""
    clock = _Clock()
    t = LogThrottle(window_s=100.0, clock=clock)

    assert t.should_log("k") is True
    for tick in range(10, 100, 10):  # nine suppressed repeats inside the window
        clock.t = float(tick)
        assert t.should_log("k") is False
    clock.t = 100.0
    assert t.should_log("k") is True, "repeats pushed the heartbeat out indefinitely"


def test_the_store_is_bounded_so_a_mis_keyed_caller_cannot_grow_without_limit():
    clock = _Clock()
    t = LogThrottle(window_s=math.inf, capacity=4, clock=clock)

    for i in range(100):
        t.should_log(f"key-{i}")

    assert len(t._seen) == 4
    # Degrades to MORE logging (the evicted key reports again), never to a leak.
    assert t.should_log("key-0") is True
    assert t.should_log("key-99") is False


def test_forget_lets_the_next_occurrence_report_immediately():
    clock = _Clock()
    t = LogThrottle(window_s=100.0, clock=clock)

    assert t.should_log("k") is True
    assert t.should_log("k") is False
    t.forget("k")
    assert t.should_log("k") is True


def test_an_infinite_window_is_a_one_shot_latch():
    """`server/sessions/claude_bin.py` depends on this degenerate case."""
    clock = _Clock()
    t = LogThrottle(window_s=math.inf, clock=clock)

    assert t.should_log("k") is True
    clock.t = 1e9
    assert t.should_log("k") is False


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR])
def test_log_throttled_demotes_repeats_to_debug_rather_than_dropping_them(caplog, level):
    """Suppressed repeats stay in the journal at DEBUG: the goal is to keep one
    stuck loop out of the WARNING+ error sink, not to hide it from someone who
    turned debug logging on to watch it."""
    log = logging.getLogger("test.logdedupe")
    t = LogThrottle(window_s=100.0, clock=_Clock())

    with caplog.at_level(logging.DEBUG, logger="test.logdedupe"):
        log_throttled(log, t, "k", level, "boom %s", "once")
        log_throttled(log, t, "k", level, "boom %s", "twice")

    levels = [r.levelno for r in caplog.records]
    assert levels == [level, logging.DEBUG]
    assert caplog.records[1].getMessage() == "boom twice"


def test_the_default_window_paces_both_incident_rates_it_was_chosen_for():
    """A window picked for the fast loop is a no-op for the slow one. #1702 ran at
    46.5 records/hour and #1852 at 0.39 — the default has to cut both, so assert
    the arithmetic rather than leaving the constant to drift on feel."""
    from server.obs.logdedupe import DEFAULT_WINDOW_S

    per_day_cap = 24 * 3600 / DEFAULT_WINDOW_S
    assert per_day_cap <= 9.3, (
        f"cap of {per_day_cap:.1f}/day does not pace #1852's measured 9.3/day")
    assert per_day_cap >= 1.0, "below one record a day the heartbeat is gone"


def test_log_throttled_carries_exc_info_through_on_the_reported_occurrence(caplog):
    log = logging.getLogger("test.logdedupe.exc")
    t = LogThrottle(window_s=100.0, clock=_Clock())
    exc = RuntimeError("cause")

    with caplog.at_level(logging.WARNING, logger="test.logdedupe.exc"):
        log_throttled(log, t, "k", logging.WARNING, "boom", exc_info=exc)

    assert caplog.records[0].exc_info is not None
