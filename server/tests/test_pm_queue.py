"""Pure ranking core (spec 003 §7a). No I/O — rows in, ordered queue + skip counts out."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.pm.desired_state import STATUS_OPTIONS
from server.pm.queue import MIN_STATUS_OPTIONS, SKIP_REASONS, QueueRow, rank, skip_reason, to_candidate

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _row(**over):
    base = dict(ref="o/r#1", repo="o/r", number=1, product="ccswitch", title="t", body="", url="u",
                status="Ready", score=10.0, gh_created_at=NOW - timedelta(days=1), gh_updated_at=NOW,
                assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
                live_session=False, claimed=False)
    base.update(over)
    return QueueRow(**base)


def test_ready_unblocked_unassigned_row_is_eligible():
    assert skip_reason(_row(), min_status="Ready", respect_dependencies=True, needs_human_retry_days=7, now=NOW) is None


def test_status_gate_honours_min_status():
    kw = dict(respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(status="Backlog"), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Backlog"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Blocked"), min_status="Backlog", **kw) == "blocked"
    assert skip_reason(_row(status="Done"), min_status="Backlog", **kw) == "status"


def test_open_blocker_blocks_unless_dependencies_are_ignored():
    kw = dict(min_status="Ready", needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=True, **kw) == "blocked"
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=False, **kw) is None


def test_live_session_claim_assignee_epic_and_bot_are_excluded():
    kw = dict(min_status="Ready", respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(live_session=True), **kw) == "live_session"
    assert skip_reason(_row(claimed=True), **kw) == "claimed"
    assert skip_reason(_row(assignees=("steiner385",)), **kw) == "assigned"
    assert skip_reason(_row(open_children=3), **kw) == "epic_open"
    assert skip_reason(_row(author="renovate[bot]"), **kw) == "bot"
    assert skip_reason(_row(labels=("dependencies",)), **kw) == "bot"
    assert skip_reason(_row(title="Dependency Dashboard"), **kw) == "bot"


def test_needs_human_label_cools_down_by_gh_updated_at():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    fresh = _row(labels=("needs-human",), gh_updated_at=NOW - timedelta(days=1))
    cooled = _row(labels=("needs-human",), gh_updated_at=NOW - timedelta(days=8))
    assert skip_reason(fresh, needs_human_retry_days=7, **kw) == "needs_human_cooldown"
    assert skip_reason(cooled, needs_human_retry_days=7, **kw) is None
    assert skip_reason(fresh, needs_human_retry_days=0, **kw) == "needs_human_cooldown"  # 0 = never retry


def test_rank_orders_score_desc_nulls_last_then_age_and_counts_skips():
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", open_blockers=("o/r#4",)),
            _row(ref="o/r#6", status="Backlog")]
    entries, skipped = rank(rows, now=NOW)
    assert [e.ref for e in entries] == ["o/r#4", "o/r#3", "o/r#1", "o/r#2"]
    assert skipped == {"blocked": 1, "status": 1}


def test_to_candidate_is_the_runner_shape_plus_provenance():
    entries, _ = rank([_row()], now=NOW)
    assert to_candidate(entries[0]) == {"ref": "o/r#1", "number": 1, "repo": "o/r", "title": "t",
                                        "body": "", "url": "u", "score": 10.0, "product": "ccswitch"}


def test_rank_rejects_unknown_min_status():
    kw = dict(respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    with pytest.raises(ValueError, match="min_status must be one of"):
        skip_reason(_row(), min_status="Nope", **kw)
    with pytest.raises(ValueError, match="min_status must be one of"):
        rank([_row()], min_status="Nope", now=NOW)


def test_min_status_options_are_the_first_two_board_statuses():
    # gh_reconcile._ensure_fields seeds the board's Status field options in
    # STATUS_OPTIONS display order (Backlog, Ready, In progress, Blocked, Needs
    # human, Done). The queue's min_status gate only ever admits from the front
    # of that order — pin the coupling so a reorder of STATUS_OPTIONS is caught
    # here instead of silently changing what "Backlog" or "Ready" mode means.
    assert MIN_STATUS_OPTIONS == STATUS_OPTIONS[:2]


def test_backlog_mode_admits_unstamped_and_foreign_statuses():
    # Decision C-2: an item the reconciler never stamped (status=None) or one
    # sitting in a non-spec column (GitHub's default "Todo") is Backlog-equivalent
    # so an unreconciled board doesn't yield an empty queue.
    kw = dict(respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(status=None), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Todo"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status=None), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Todo"), min_status="Ready", **kw) == "status"


def test_every_skip_reason_is_in_the_vocabulary():
    kw = dict(min_status="Ready", respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    cases = {
        "status": _row(status="Done"),
        "blocked": _row(status="Blocked"),
        "live_session": _row(live_session=True),
        "claimed": _row(claimed=True),
        "assigned": _row(assignees=("steiner385",)),
        "epic_open": _row(open_children=1),
        "bot": _row(author="renovate[bot]"),
        "needs_human_cooldown": _row(labels=("needs-human",), gh_updated_at=NOW - timedelta(days=1)),
    }
    seen = set()
    for expected, row in cases.items():
        got = skip_reason(row, **kw)
        assert got == expected
        seen.add(got)
    assert seen == set(SKIP_REASONS)
