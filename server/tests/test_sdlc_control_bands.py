"""Pure-detector tests for the SDLC Maintain loop (#804).

No DB, no network, no clock — ``server.sdlc.control_bands`` is a total function
over a list of samples, so every rule is pinned exhaustively here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.sdlc.control_bands import (
    EVAL_POINTS,
    Baseline,
    Sample,
    bucket_daily,
    compute_baseline,
    evaluate,
)


def _series(values: list[float]) -> list[Sample]:
    """Samples labelled 2026-01-01, -02, … so ordering is obvious in failures."""
    return [Sample(at=f"2026-01-{i + 1:02d}", value=v) for i, v in enumerate(values)]


def _stable(n: int = 30) -> list[float]:
    """A baseline with real, small variation (σ > 0) and mean 10."""
    return [10.0 + (0.5 if i % 2 else -0.5) for i in range(n)]


# --------------------------------------------------------------------------- #
# bucket_daily                                                                  #
# --------------------------------------------------------------------------- #

def test_bucket_daily_averages_within_a_utc_day_and_sorts_oldest_first():
    obs = [
        (datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc), 4.0),
        (datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc), 1.0),
        (datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc), 6.0),
    ]
    assert bucket_daily(obs) == [
        Sample(at="2026-01-01", value=1.0),
        Sample(at="2026-01-02", value=5.0),
    ]


def test_bucket_daily_reads_naive_timestamps_as_utc():
    """Every timestamp in this codebase's DB is naive UTC (server/db/clock.py)."""
    naive = bucket_daily([(datetime(2026, 1, 1, 12, 0), 3.0)])
    aware = bucket_daily([(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), 3.0)])
    assert naive == aware == [Sample(at="2026-01-01", value=3.0)]


def test_bucket_daily_skips_empty_days_rather_than_inventing_zeros():
    obs = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc), 5.0),
        (datetime(2026, 1, 5, tzinfo=timezone.utc), 5.0),
    ]
    assert [s.at for s in bucket_daily(obs)] == ["2026-01-01", "2026-01-05"]


def test_bucket_daily_of_nothing_is_nothing():
    assert bucket_daily([]) == []


# --------------------------------------------------------------------------- #
# compute_baseline                                                              #
# --------------------------------------------------------------------------- #

def test_baseline_excludes_the_evaluation_points():
    """The points under test must not shape the band they are measured against —
    otherwise a drift normalises itself away."""
    samples = _series([1.0] * 20 + [99.0] * EVAL_POINTS)
    base = compute_baseline(samples, baseline_points=30)
    assert base is not None
    assert base.n == 20
    assert base.mean == pytest.approx(1.0)


def test_baseline_is_rolling_capped_at_baseline_points():
    samples = _series([0.0] * 50 + [5.0] * 10 + [0.0] * EVAL_POINTS)
    base = compute_baseline(samples, baseline_points=10)
    assert base is not None
    assert base.n == 10
    assert base.mean == pytest.approx(5.0)


def test_baseline_is_none_when_there_is_nothing_before_the_eval_window():
    assert compute_baseline(_series([1.0] * EVAL_POINTS), baseline_points=30) is None


def test_baseline_uses_population_stddev():
    base = compute_baseline(_series([2.0, 4.0] + [0.0] * EVAL_POINTS),
                            baseline_points=30)
    assert base is not None
    # pstdev([2, 4]) == 1.0; stdev (sample form) would be ~1.414.
    assert base.stddev == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# evaluate — refusals                                                           #
# --------------------------------------------------------------------------- #

def test_no_breach_when_the_series_is_too_short():
    assert evaluate("m", _series([1.0] * EVAL_POINTS), min_baseline=1) is None


def test_no_breach_when_the_baseline_is_thinner_than_min_baseline():
    samples = _series(_stable(5) + [1000.0] * EVAL_POINTS)
    assert evaluate("m", samples, min_baseline=10) is None
    assert evaluate("m", samples, min_baseline=5) is not None


def test_flat_baseline_never_breaches():
    """σ = 0 makes every non-mean value infinitely many σ out. A constant metric
    that moves at all must not report a 3σ emergency."""
    samples = _series([1.0] * 20 + [1.0] * (EVAL_POINTS - 1) + [1.001])
    assert evaluate("m", samples, min_baseline=10) is None


def test_in_control_series_does_not_breach():
    assert evaluate("m", _series(_stable(40)), min_baseline=10) is None


def test_unknown_direction_raises_rather_than_defaulting():
    with pytest.raises(ValueError, match="unknown direction"):
        evaluate("m", _series(_stable(40)), direction="sideways")


# --------------------------------------------------------------------------- #
# evaluate — the Western Electric rules                                         #
# --------------------------------------------------------------------------- #

def test_we1_single_point_beyond_3_sigma():
    samples = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [50.0])
    breach = evaluate("m", samples, min_baseline=10)
    assert breach is not None
    assert (breach.rule, breach.sigma, breach.side) == ("we1", 3, "high")
    assert breach.at == samples[-1].at
    assert breach.value == 50.0
    assert breach.points == (samples[-1],)


def test_we2_two_of_three_beyond_2_sigma():
    # Baseline mean 10, σ 0.5. 11.4 is +2.8σ (past 2σ, inside 3σ).
    samples = _series(_stable(30) + [10.0] * 5 + [11.4, 10.0, 11.4])
    breach = evaluate("m", samples, min_baseline=10)
    assert breach is not None
    assert (breach.rule, breach.sigma) == ("we2", 2)
    assert len(breach.points) == 3


def test_we3_four_of_five_beyond_1_sigma():
    # 10.8 is +1.6σ: past 1σ, inside 2σ.
    samples = _series(_stable(30) + [10.0] * 3 + [10.8, 10.8, 10.0, 10.8, 10.8])
    breach = evaluate("m", samples, min_baseline=10)
    assert breach is not None
    assert (breach.rule, breach.sigma) == ("we3", 1)
    assert len(breach.points) == 5


def test_we4_eight_consecutive_on_one_side_of_the_mean():
    # Every point above the mean but none past 1σ — only the run rule can see it.
    samples = _series(_stable(30) + [10.2] * EVAL_POINTS)
    breach = evaluate("m", samples, min_baseline=10)
    assert breach is not None
    assert (breach.rule, breach.sigma) == ("we4", 1)
    assert len(breach.points) == EVAL_POINTS


def test_the_most_severe_rule_wins():
    """A 3σ point is also beyond 2σ and 1σ; it must report as we1."""
    samples = _series(_stable(30) + [11.4] * (EVAL_POINTS - 1) + [50.0])
    breach = evaluate("m", samples, min_baseline=10)
    assert breach is not None and breach.rule == "we1"


# --------------------------------------------------------------------------- #
# evaluate — anchoring and direction                                            #
# --------------------------------------------------------------------------- #

def test_a_recovered_excursion_stops_breaching():
    """Rules are anchored on the newest sample, so a spike that has passed does
    not re-fire forever while it sits inside the evaluation window."""
    spiked = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [50.0])
    assert evaluate("m", spiked, min_baseline=10) is not None
    recovered = _series(_stable(30) + [50.0] + [10.0] * (EVAL_POINTS - 1))
    assert evaluate("m", recovered, min_baseline=10) is None


def test_direction_filters_the_side_that_counts():
    low = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [-50.0])
    assert evaluate("m", low, direction="higher_is_worse", min_baseline=10) is None
    breach = evaluate("m", low, direction="lower_is_worse", min_baseline=10)
    assert breach is not None and breach.side == "low"
    assert evaluate("m", low, direction="both", min_baseline=10) is not None


def test_severity_dominates_the_side_sweep_under_direction_both():
    """With `both`, a 3σ low must beat a 1σ high — the rule loop is outermost."""
    samples = _series(_stable(30) + [10.2] * (EVAL_POINTS - 1) + [-50.0])
    breach = evaluate("m", samples, direction="both", min_baseline=10)
    assert breach is not None
    assert (breach.rule, breach.side) == ("we1", "low")


# --------------------------------------------------------------------------- #
# Breach identity + rendering                                                   #
# --------------------------------------------------------------------------- #

def test_breach_key_is_stable_across_ticks_and_splits_on_rule_or_side():
    samples = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [50.0])
    first = evaluate("pr_cycle_time_hours", samples, min_baseline=10)
    again = evaluate("pr_cycle_time_hours", samples, min_baseline=10)
    assert first is not None and again is not None
    assert first.key() == again.key() == "pr_cycle_time_hours:we1:high"

    low = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [-50.0])
    other = evaluate("pr_cycle_time_hours", low, direction="both", min_baseline=10)
    assert other is not None and other.key() != first.key()


def test_breach_summary_names_the_metric_rule_and_baseline():
    samples = _series(_stable(30) + [10.0] * (EVAL_POINTS - 1) + [50.0])
    breach = evaluate("pr_cycle_time_hours", samples, min_baseline=10)
    assert breach is not None
    text = breach.summary()
    assert "pr_cycle_time_hours" in text
    assert "we1" in text and "3σ" in text and "above" in text
    assert breach.z > 3


def test_baseline_zone_is_zero_for_a_flat_baseline():
    assert Baseline(mean=1.0, stddev=0.0, n=5).zone(99.0) == 0.0
