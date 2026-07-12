"""mergeEtaSim pure-simulator unit tests (SPEC §1 — plotroom queue.ts port).

Pins the analytic multi-train ETA math against plotroom@5166b52
server/estimator/queue.ts:107-167 + server/math.ts:6-8 (percentile). No DB.
"""
from server.pipeline.merge_sim import (
    EJECT_BUMP_MIN_PROB,
    EJECT_PROB_MIN_SAMPLES,
    eject_probability,
    simulate_merge_eta,
)


def test_no_samples_returns_none():
    assert simulate_merge_eta(0, 6, [], 0.0, None) is None


def test_non_positive_samples_filtered_to_none():
    # plotroom filters d > 0 && finite; an all-zero list has no usable samples.
    assert simulate_merge_eta(0, 6, [0, -5], 0.0, None) is None


def test_single_train_no_eject_bump():
    # One sample => p50 == p90 == that value; trains = ceil(1/6) = 1, no bump.
    sim = simulate_merge_eta(0, 6, [600], 0.0, None)
    assert sim == {
        "p50Secs": 600,
        "p90Secs": 600,
        "trainsAhead": 0,
        "assumesEjects": False,
    }


def test_eject_bump_above_threshold_adds_a_train_to_p90():
    # ejectProb 0.2 > 0.15 => p90 gets (trains + 1) * dur90.
    sim = simulate_merge_eta(0, 6, [600], 0.2, None)
    assert sim["p50Secs"] == 600          # 1 * 600
    assert sim["p90Secs"] == 1200         # (1 + 1) * 600
    assert sim["assumesEjects"] is True


def test_eject_bump_at_threshold_is_no_bump():
    # Strict > 0.15: exactly 0.15 does NOT bump.
    sim = simulate_merge_eta(0, 6, [600], EJECT_BUMP_MIN_PROB, None)
    assert sim["p90Secs"] == 600
    assert sim["assumesEjects"] is False


def test_trains_ahead_without_building_train():
    # queuedAhead 6 => trains = ceil(7/6) = 2; no building train => trainsAhead = 1.
    sim = simulate_merge_eta(6, 6, [100], 0.0, None)
    assert sim["trainsAhead"] == 1        # trains - 1 + 0
    assert sim["p50Secs"] == 200          # 2 * 100


def test_trains_ahead_with_building_train_adds_current_eta():
    # currentTrainEta present => trainsAhead gets +1 and ETA gets the base.
    sim = simulate_merge_eta(0, 6, [100], 0.0, 300)
    assert sim["trainsAhead"] == 1        # trains(1) - 1 + 1
    assert sim["p50Secs"] == 400          # 300 + 1 * 100
    assert sim["p90Secs"] == 400


def test_negative_current_eta_clamped_to_zero():
    sim = simulate_merge_eta(0, 6, [100], 0.0, -50)
    # currentEta clamped to 0, but trainsAhead still +1 (currentTrainEta != null).
    assert sim["p50Secs"] == 100
    assert sim["trainsAhead"] == 1


def test_batch_size_floored_at_one():
    # batchSize 0 must not divide-by-zero; floored to 1 => trains = ceil(4/1) = 4.
    sim = simulate_merge_eta(3, 0, [100], 0.0, None)
    assert sim["trainsAhead"] == 3        # 4 - 1


def test_plotroom_percentile_pick_on_fixed_20_sample_array():
    # SPEC §1 governs: percentile(sorted, p) = sorted[min(len-1, ceil(p*len)-1)]
    # (plotroom server/math.ts:6-8). For sorted [1..20]:
    #   p50 -> min(19, ceil(0.5*20)-1) = min(19, 9)  -> sorted[9]  = 10
    #   p90 -> min(19, ceil(0.9*20)-1) = min(19, 17) -> sorted[17] = 18
    samples = list(range(20, 0, -1))      # 20..1 unsorted (newest-first shape)
    sim = simulate_merge_eta(0, 6, samples, 0.0, None)
    assert sim["p50Secs"] == 10           # dur50 pick
    assert sim["p90Secs"] == 18           # dur90 pick


def test_eject_probability_below_min_samples_is_zero():
    # total < 5 => 0.0 regardless of ratio.
    assert eject_probability(2, 2) == 0.0          # total 4
    assert EJECT_PROB_MIN_SAMPLES == 5


def test_eject_probability_at_and_above_min_samples():
    assert eject_probability(3, 2) == 0.4          # total 5 => ejects/total
    assert eject_probability(9, 1) == 0.1
    assert eject_probability(0, 0) == 0.0
