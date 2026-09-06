"""Pure pm-triage engine: next_triage_action over the (state, counts, mode) table.

Exhaustive, I/O-free — the way test_sdlc_engine tests next_action. Every branch
of the priority ladder (classify → drift → stale → idle) plus the off / cap /
threshold edges, per design §6.
"""
from __future__ import annotations

from server.pm.engine import (
    ClassifyBatch,
    Idle,
    NeedsHuman,
    NoFitCluster,
    NudgeStale,
    ProposeFeatureMapDiff,
    StaleFeature,
    TriageState,
    next_triage_action,
)

_DEFAULT_CONFIG = {"drift_threshold": 5, "batch_size": 50, "stale_days": 14}


def _state(**kw) -> TriageState:
    base = dict(mode="propose", unclassified_count=0, pending_count=0,
                no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    base.update(kw)
    return TriageState(**base)  # type: ignore[arg-type]


def _cluster(suggestion="realtime-sync", n=5, repo="o/r") -> NoFitCluster:
    return NoFitCluster(suggestion=suggestion,
                        item_numbers=tuple(range(1, n + 1)), repo=repo)


# --------------------------------------------------------------------------- #
# Off / terminal                                                              #
# --------------------------------------------------------------------------- #

def test_off_mode_needs_human():
    act = next_triage_action(_state(mode="off", unclassified_count=9), _DEFAULT_CONFIG)
    assert isinstance(act, NeedsHuman)


def test_empty_queue_is_idle():
    assert next_triage_action(_state(), _DEFAULT_CONFIG) == Idle()


# --------------------------------------------------------------------------- #
# Classify-the-new batching (design §6.1) — highest priority when work exists #
# --------------------------------------------------------------------------- #

def test_unclassified_backlog_classifies_a_capped_batch():
    act = next_triage_action(_state(unclassified_count=120), _DEFAULT_CONFIG)
    assert act == ClassifyBatch(count=50)  # bounded by batch_size


def test_batch_bounded_by_remaining_when_smaller_than_batch_size():
    act = next_triage_action(_state(unclassified_count=7), _DEFAULT_CONFIG)
    assert act == ClassifyBatch(count=7)


def test_day_cap_reached_defers_classify_but_still_drifts():
    # At the daily session cap, no ClassifyBatch — fall through to drift.
    st = _state(unclassified_count=50, day_spawns=8, day_cap=8,
                no_fit_clusters=(_cluster(n=5),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert isinstance(act, ProposeFeatureMapDiff)


def test_pending_proposals_do_not_reclassify():
    # Items already have pending proposals; unclassified_count is the loop's
    # "no pending proposal" count, so a pure pending queue is idle here.
    assert next_triage_action(_state(pending_count=40), _DEFAULT_CONFIG) == Idle()


# --------------------------------------------------------------------------- #
# Drift clustering (design §6.3)                                              #
# --------------------------------------------------------------------------- #

def test_cluster_at_threshold_proposes_map_diff():
    st = _state(no_fit_clusters=(_cluster(suggestion="offline-mode", n=5),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == ProposeFeatureMapDiff(
        suggestion="offline-mode", item_numbers=(1, 2, 3, 4, 5), repo="o/r")


def test_cluster_below_threshold_does_not_propose():
    st = _state(no_fit_clusters=(_cluster(n=4),))  # 4 < 5
    assert next_triage_action(st, _DEFAULT_CONFIG) == Idle()


def test_largest_cluster_wins_when_several_qualify():
    st = _state(no_fit_clusters=(_cluster("a", n=5), _cluster("b", n=9, repo="o/r2")))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert isinstance(act, ProposeFeatureMapDiff) and act.suggestion == "b"


def test_classify_outranks_drift_when_both_available():
    st = _state(unclassified_count=10, no_fit_clusters=(_cluster(n=9),))
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=10)


# --------------------------------------------------------------------------- #
# Staleness nudge (design §6.4) — indicator only, lowest non-idle priority     #
# --------------------------------------------------------------------------- #

def test_stale_features_nudge_when_nothing_else_to_do():
    st = _state(stale_features=(
        StaleFeature(slug="billing", state="delivering", days_idle=30),
        StaleFeature(slug="search", state="discovery", days_idle=21)))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == NudgeStale(slugs=("billing", "search"))


def test_stale_ignored_while_classify_or_drift_pending():
    st = _state(unclassified_count=3,
                stale_features=(StaleFeature("x", "delivering", 99),))
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=3)
