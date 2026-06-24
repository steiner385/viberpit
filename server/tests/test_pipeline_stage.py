"""Unit tests for the open-stage classifier (server/pipeline/stage.py)."""
from server.pipeline.stage import derive_open_stage


def test_draft_is_parked():
    assert derive_open_stage("passing", None, is_draft=True) == "parked"


def test_draft_is_parked_regardless_of_checks():
    assert derive_open_stage("pending", "CLEAN", is_draft=True) == "parked"


def test_failing_checks_is_parked():
    assert derive_open_stage("failing", None, is_draft=False) == "parked"


def test_pending_checks_is_ci():
    assert derive_open_stage("pending", None, is_draft=False) == "ci"


def test_pending_checks_clean_is_ci():
    assert derive_open_stage("pending", "CLEAN", is_draft=False) == "ci"


def test_passing_dirty_is_parked():
    assert derive_open_stage("passing", "DIRTY", is_draft=False) == "parked"


def test_passing_clean_is_ready():
    assert derive_open_stage("passing", "CLEAN", is_draft=False) == "ready"


def test_none_checks_clean_is_ready():
    assert derive_open_stage("none", None, is_draft=False) == "ready"


def test_none_checks_dirty_is_parked():
    assert derive_open_stage("none", "DIRTY", is_draft=False) == "parked"


def test_passing_no_merge_state_is_ready():
    # None merge_state_status (e.g. API not yet returned) → ready (not DIRTY)
    assert derive_open_stage("passing", None, is_draft=False) == "ready"
