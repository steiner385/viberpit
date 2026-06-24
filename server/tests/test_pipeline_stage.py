"""Unit tests for the open-stage classifier (server/pipeline/stage.py)."""
from datetime import datetime, timezone, timedelta

from server.pipeline.stage import derive_open_stage, derive_queue_stage, derive_merged_stage


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


# --- Merged stage ---

_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_merged_within_retention_returns_merged():
    merged_at = (_NOW - timedelta(days=2)).isoformat()
    assert derive_merged_stage(merged_at, _NOW) == "merged"


def test_merged_older_than_retention_returns_none():
    merged_at = (_NOW - timedelta(days=8)).isoformat()
    assert derive_merged_stage(merged_at, _NOW) is None


def test_merged_none_merged_at_returns_none():
    assert derive_merged_stage(None, _NOW) is None


def test_merged_with_trailing_z_parses():
    merged_at = "2026-06-23T10:00:00Z"
    assert derive_merged_stage(merged_at, _NOW) == "merged"


def test_merged_has_deploy_returns_none():
    # has_deploy=True: deploy stages not implemented (TODO slice 1d)
    merged_at = (_NOW - timedelta(days=1)).isoformat()
    assert derive_merged_stage(merged_at, _NOW, has_deploy=True) is None


def test_merged_exactly_at_retention_boundary():
    # age == retention_days: still on board (> not >=)
    merged_at = (_NOW - timedelta(days=7)).isoformat()
    assert derive_merged_stage(merged_at, _NOW) == "merged"


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
