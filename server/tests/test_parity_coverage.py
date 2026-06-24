import pytest

from server.github.parity.coverage import covered_fields


def test_phase0_covers_only_oracle_real_fields():
    # Phase 0 covers exactly the fields the live Plotroom /api/state emits per PR.
    f = covered_fields(0)
    assert f == {"number", "url", "checks"}
    # Fields Plotroom does NOT expose (or that are a different vocabulary) stay out
    # until later phases; stage/ETA are never Phase-0:
    for absent in ("head_sha", "is_draft", "state", "checks_detail", "eta_secs", "stage"):
        assert absent not in f


def test_unknown_phase_raises():
    with pytest.raises(KeyError):
        covered_fields(99)
