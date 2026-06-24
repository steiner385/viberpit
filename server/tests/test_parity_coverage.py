import pytest

from server.github.parity.coverage import covered_fields


def test_phase0_covers_basic_pr_fields():
    f = covered_fields(0)
    assert {"number", "url", "state", "checks", "checks_detail"} <= f
    # Stage/ETA fields are explicitly NOT covered in phase 0:
    assert "eta_secs" not in f and "stage" not in f


def test_unknown_phase_raises():
    with pytest.raises(KeyError):
        covered_fields(99)
