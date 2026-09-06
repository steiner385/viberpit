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
    qualifying_clusters,
)

_DEFAULT_CONFIG = {"drift_threshold": 5, "batch_size": 50, "stale_days": 14}


def _state(**kw) -> TriageState:
    base = dict(mode="propose", unclassified_count=0, pending_count=0,
                no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8,
                open_map_diff_suggestions=())
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
    assert act.reason  # the loop emits this as feed text — never bare


def test_off_mode_outranks_drift_and_stale():
    # Kill switch: with the automation off, a qualifying drift cluster must NOT
    # open a PR and stale features must NOT be nudged. Pins the ORDER of the
    # off-check, which the unclassified-only case cannot.
    st = _state(mode="off", unclassified_count=0,
                no_fit_clusters=(_cluster(n=9),),
                stale_features=(StaleFeature("billing", "delivering", 30),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert isinstance(act, NeedsHuman)
    assert act.reason


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


def test_last_slot_under_the_day_cap_still_classifies():
    # Guards an off-by-one (``day_spawns + 1 < day_cap``) that would silently
    # burn one fewer session per day than configured.
    st = _state(unclassified_count=50, day_spawns=7, day_cap=8)
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=50)


def test_backlog_exactly_batch_size_classifies_the_whole_batch():
    act = next_triage_action(_state(unclassified_count=50), _DEFAULT_CONFIG)
    assert act == ClassifyBatch(count=50)


def test_pending_count_is_informational_and_does_not_drive_the_decision():
    # ``pending_count`` is surfaced in status only — the engine never reads it,
    # so a pure pending queue decides exactly as an empty one does.
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


def test_equal_size_clusters_break_the_tie_deterministically():
    # Cluster order comes from an unordered SELECT, so two equal-size clusters
    # must not alternate between ticks. Same input, either order → same winner.
    a, b = _cluster("alpha", n=6), _cluster("beta", n=6, repo="o/r2")
    first = next_triage_action(_state(no_fit_clusters=(a, b)), _DEFAULT_CONFIG)
    second = next_triage_action(_state(no_fit_clusters=(b, a)), _DEFAULT_CONFIG)
    assert first == second
    assert isinstance(first, ProposeFeatureMapDiff) and first.suggestion == "beta"


def test_classify_outranks_drift_when_both_available():
    st = _state(unclassified_count=10, no_fit_clusters=(_cluster(n=9),))
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=10)


# --------------------------------------------------------------------------- #
# Drift latch — a capability is proposed ONCE, never re-proposed every tick    #
# --------------------------------------------------------------------------- #

def test_latched_suggestion_does_not_re_propose():
    # ``no_fit`` clusters never shrink, so without the latch this cluster would
    # open a fresh archaeology-diff PR on every tick forever.
    st = _state(no_fit_clusters=(_cluster("offline-mode", n=9),),
                open_map_diff_suggestions=("offline-mode",))
    assert next_triage_action(st, _DEFAULT_CONFIG) == Idle()


def test_latched_suggestion_falls_through_to_stale():
    st = _state(no_fit_clusters=(_cluster("offline-mode", n=9),),
                open_map_diff_suggestions=("offline-mode",),
                stale_features=(StaleFeature("billing", "delivering", 30),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == NudgeStale(slugs=("billing",))


def test_unlatched_cluster_still_wins_when_a_larger_one_is_latched():
    st = _state(no_fit_clusters=(_cluster("offline-mode", n=9),
                                 _cluster("realtime-sync", n=5, repo="o/r2")),
                open_map_diff_suggestions=("offline-mode",))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == ProposeFeatureMapDiff(
        suggestion="realtime-sync", item_numbers=(1, 2, 3, 4, 5), repo="o/r2")


# --------------------------------------------------------------------------- #
# qualifying_clusters — the ONE definition of "this cluster warrants a diff"    #
# --------------------------------------------------------------------------- #

def test_qualifying_clusters_applies_threshold_and_latch():
    """The status card counts drift too. If it re-implements the rule it drifts
    from the engine — notably by omitting the latch, which pins the card at
    "N drift" forever once every cluster has been proposed."""
    st = _state(no_fit_clusters=(_cluster("under", n=4),
                                 _cluster("latched", n=9),
                                 _cluster("open", n=5, repo="o/r2")),
                open_map_diff_suggestions=("latched",))
    assert tuple(c.suggestion for c in qualifying_clusters(st, _DEFAULT_CONFIG)) \
        == ("open",)


def test_qualifying_clusters_honours_a_custom_threshold():
    st = _state(no_fit_clusters=(_cluster("small", n=2),))
    assert qualifying_clusters(st, {"drift_threshold": 2}) == st.no_fit_clusters
    assert qualifying_clusters(st, {"drift_threshold": 3}) == ()


def test_next_triage_action_proposes_only_qualifying_clusters():
    # The decision and the count are the same rule: whatever the helper returns
    # empty for, the engine must not propose.
    st = _state(no_fit_clusters=(_cluster("latched", n=9),),
                open_map_diff_suggestions=("latched",))
    assert qualifying_clusters(st, _DEFAULT_CONFIG) == ()
    assert next_triage_action(st, _DEFAULT_CONFIG) == Idle()


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
