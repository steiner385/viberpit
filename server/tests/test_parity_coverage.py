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


def test_phase1_adds_stage():
    # Phase 1 extends Phase 0 with the open-stage classifier output.
    f0 = covered_fields(0)
    f1 = covered_fields(1)
    assert f0 < f1, "phase 1 must be a strict superset of phase 0"
    assert "stage" in f1
    # Phase 0 fields must remain in phase 1
    for name in f0:
        assert name in f1


def test_phase1_is_phase0_union_stage():
    f0 = covered_fields(0)
    f1 = covered_fields(1)
    assert f1 == f0 | frozenset({"stage"})


def test_unknown_phase_raises():
    with pytest.raises(KeyError):
        covered_fields(99)
