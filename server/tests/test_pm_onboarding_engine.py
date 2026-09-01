"""Pure onboarding state-machine engine: next_onboarding_action over the
registered→syncing→map_proposed→map_merged→classifying→live wizard states.

Exhaustive, I/O-free — the way test_sdlc_engine tests next_action. The wizard is
a VIEW over this engine (spec §5); the transitions here are the single source of
truth for which action each state offers.
"""
from __future__ import annotations

import pytest

from server.db.models import PROJECT_ONBOARDING_STATES
from server.pm.onboarding import (
    AwaitBackfill,
    AwaitMapPr,
    AwaitTriage,
    Classify,
    Live,
    OnboardingNeedsHuman,
    ProjectOnboardingView,
    ProposeMap,
    Register,
    advance_onboarding_state,
    next_onboarding_action,
)


def _view(**kw) -> ProjectOnboardingView:
    base = dict(state="syncing", repo_count=1, backfill_done_count=0,
                feature_count=0, open_unclassified_count=0,
                pending_proposal_count=0, map_pr_open=False, map_pr_merged=False)
    base.update(kw)
    return ProjectOnboardingView(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# registered: nothing attached yet → Register (needs at least one repo)        #
# --------------------------------------------------------------------------- #

def test_registered_with_no_repos_is_register():
    v = _view(state="registered", repo_count=0)
    assert next_onboarding_action(v) == Register()


def test_registered_with_repos_awaits_backfill():
    # A registered project with repos is waiting for pm-sync to backfill.
    v = _view(state="registered", repo_count=1, backfill_done_count=0)
    assert next_onboarding_action(v) == AwaitBackfill()


# --------------------------------------------------------------------------- #
# syncing: backfill in flight → wait; all repos done → propose the map         #
# --------------------------------------------------------------------------- #

def test_syncing_partial_backfill_awaits():
    v = _view(state="syncing", repo_count=2, backfill_done_count=1)
    assert next_onboarding_action(v) == AwaitBackfill()


def test_syncing_all_backfilled_proposes_map():
    v = _view(state="syncing", repo_count=2, backfill_done_count=2)
    assert next_onboarding_action(v) == ProposeMap()


# --------------------------------------------------------------------------- #
# map_proposed: PR open → wait; PR merged → the human advance moves state on    #
# --------------------------------------------------------------------------- #

def test_map_proposed_pr_open_awaits():
    v = _view(state="map_proposed", backfill_done_count=1, map_pr_open=True)
    assert next_onboarding_action(v) == AwaitMapPr()


def test_map_proposed_pr_merged_still_awaits_human_advance():
    # Merge is the gate; the engine never advances the state itself (propose-only
    # parity with the SDLC engine). It keeps offering AwaitMapPr until advanced.
    v = _view(state="map_proposed", backfill_done_count=1,
              map_pr_open=False, map_pr_merged=True)
    assert next_onboarding_action(v) == AwaitMapPr()


def test_map_proposed_no_pr_yet_reproposes():
    # Re-entering the state with no PR (e.g. session died) re-offers ProposeMap.
    v = _view(state="map_proposed", map_pr_open=False, map_pr_merged=False)
    assert next_onboarding_action(v) == ProposeMap()


# --------------------------------------------------------------------------- #
# map_merged: features indexed → classify the open backlog                      #
# --------------------------------------------------------------------------- #

def test_map_merged_with_features_classifies():
    v = _view(state="map_merged", feature_count=6, open_unclassified_count=40)
    assert next_onboarding_action(v) == Classify()


def test_map_merged_without_features_needs_human():
    # Map "merged" but no Feature rows indexed = something went wrong upstream.
    v = _view(state="map_merged", feature_count=0)
    assert isinstance(next_onboarding_action(v), OnboardingNeedsHuman)


def test_map_merged_nothing_to_classify_goes_live_signal():
    # No open unclassified items and none pending: classification is a no-op, so
    # the queue is effectively drained — offer AwaitTriage (the advance to live).
    v = _view(state="map_merged", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=0)
    assert next_onboarding_action(v) == AwaitTriage()


# --------------------------------------------------------------------------- #
# classifying: queue draining → wait; drained → advance to live                 #
# --------------------------------------------------------------------------- #

def test_classifying_with_unclassified_reclassifies():
    v = _view(state="classifying", feature_count=3, open_unclassified_count=12)
    assert next_onboarding_action(v) == Classify()


def test_classifying_only_pending_awaits_triage():
    # Everything is proposed; the human must drain the triage queue.
    v = _view(state="classifying", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=8)
    assert next_onboarding_action(v) == AwaitTriage()


def test_classifying_queue_drained_awaits_triage_then_live():
    v = _view(state="classifying", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=0)
    assert next_onboarding_action(v) == AwaitTriage()


# --------------------------------------------------------------------------- #
# live: terminal                                                                #
# --------------------------------------------------------------------------- #

def test_live_is_live():
    assert next_onboarding_action(_view(state="live")) == Live()


def test_unknown_state_needs_human():
    assert isinstance(next_onboarding_action(_view(state="bogus")),
                      OnboardingNeedsHuman)


# --------------------------------------------------------------------------- #
# advance_onboarding_state                                                       #
# --------------------------------------------------------------------------- #

def test_advance_walks_the_sequence():
    assert advance_onboarding_state("registered") == "syncing"
    assert advance_onboarding_state("map_proposed") == "map_merged"
    assert advance_onboarding_state("classifying") == "live"
    assert advance_onboarding_state("live") == "live"  # absorbing


def test_advance_unknown_raises():
    with pytest.raises(ValueError):
        advance_onboarding_state("bogus")


# --------------------------------------------------------------------------- #
# Purity / determinism / constants                                              #
# --------------------------------------------------------------------------- #

def test_deterministic_and_does_not_mutate():
    v = _view(state="syncing", repo_count=2, backfill_done_count=2)
    before = v
    a = next_onboarding_action(v)
    b = next_onboarding_action(v)
    assert a == b == ProposeMap()
    assert v == before  # frozen dataclass — nothing mutated


def test_states_match_phase1_constant():
    assert PROJECT_ONBOARDING_STATES == (
        "registered", "syncing", "map_proposed", "map_merged", "classifying", "live")
