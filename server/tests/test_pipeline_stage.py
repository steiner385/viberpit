"""Unit tests for the open-stage classifier (server/pipeline/stage.py)."""
from server.pipeline.stage import derive_open_stage, derive_queue_stage


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


# --- Queue stage (slice 1b — unit-tested; not live-parity-gated on a no-merge-queue repo) ---

def test_queue_unmergeable_dirty_is_unmergeable():
    # The PR's own base conflict (DIRTY) → genuine 'unmergeable'.
    assert derive_queue_stage(queue_state="UNMERGEABLE", merge_state_status="DIRTY") == (
        "queue", "unmergeable",
    )


def test_queue_unmergeable_nondirty_is_queue_blocked():
    # UNMERGEABLE but not DIRTY = cascade victim poisoned by a conflicting entry ahead.
    assert derive_queue_stage(queue_state="UNMERGEABLE", merge_state_status="BLOCKED") == (
        "queue", "queue-blocked",
    )


def test_queue_unmergeable_flag_without_entry_state():
    # The queueProgress 'unmergeable' signal alone suffices (refreshes on a different cadence).
    assert derive_queue_stage(unmergeable=True, merge_state_status="UNKNOWN") == (
        "queue", "queue-blocked",
    )


def test_queue_group_failed():
    assert derive_queue_stage(group_failed=True) == ("queue", "group-failed")


def test_queue_progressing_has_no_substate():
    assert derive_queue_stage() == ("queue", None)


def test_queue_unmergeable_precedence_over_group_failed():
    # Ejection risk takes precedence over a group-build failure substate.
    assert derive_queue_stage(
        queue_state="UNMERGEABLE", merge_state_status="DIRTY", group_failed=True
    ) == ("queue", "unmergeable")
