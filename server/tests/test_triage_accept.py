"""server/pm/triage_accept: the pure auto-accept gate (spec §8, §13)."""
from __future__ import annotations

import pytest

from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.triage_accept import auto_accept_eligible


def _ok(**over):
    base = dict(tier="high", confidence_level="high", proposed_feature_id="f1",
                feature_state="live", auto_undone=None)
    base.update(over)
    return auto_accept_eligible(**base)


def test_confidence_levels_are_the_one_ascending_vocabulary():
    assert CONFIDENCE_LEVELS == ("low", "medium", "high")


def test_high_tier_accepts_only_high():
    assert _ok(tier="high", confidence_level="high")
    assert not _ok(tier="high", confidence_level="medium")
    assert not _ok(tier="high", confidence_level="low")


def test_medium_tier_includes_high():
    assert _ok(tier="medium", confidence_level="medium")
    assert _ok(tier="medium", confidence_level="high")
    assert not _ok(tier="medium", confidence_level="low")


def test_off_never_accepts():
    assert not _ok(tier="off", confidence_level="high")


@pytest.mark.parametrize("level", [None, "", "HIGH", "certain", "very high"])
def test_a_confidence_outside_the_vocabulary_reads_as_low(level):
    # Spec §13: vocabulary drift is treated as low, never accepted.
    assert not _ok(tier="medium", confidence_level=level)


def test_never_a_no_fit_proposal():
    assert not _ok(proposed_feature_id=None)


def test_never_a_proposal_whose_capability_is_still_proposed():
    assert not _ok(feature_state="proposed")
    assert _ok(feature_state="discovery")
    assert _ok(feature_state="delivering")


def test_a_missing_feature_state_is_not_eligible():
    # The target row is gone (or unknown): a human decides, not the machine.
    assert not _ok(feature_state=None)


def test_an_undone_auto_accept_is_never_retried():
    assert not _ok(auto_undone=True)
    assert _ok(auto_undone=False)
    assert _ok(auto_undone=None)
