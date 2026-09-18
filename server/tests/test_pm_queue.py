"""Pure ranking core (spec 003 §7a). No I/O — rows in, ordered queue + skip counts out."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.pm.desired_state import STATUS_OPTIONS, STATUS_READY
from server.pm.queue import (MIN_STATUS_OPTIONS, SKIP_REASONS, URGENT_LABELS, QueueRow, rank,
                             rank_key, skip_reason, to_candidate)

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _row(**over):
    base = dict(ref="o/r#1", repo="o/r", number=1, product="viberpit", title="t", body="", url="u",
                status="Ready", score=10.0, gh_created_at=NOW - timedelta(days=1), gh_updated_at=NOW,
                assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
                live_session=False, claimed=False, hold_kind=None)
    base.update(over)
    return QueueRow(**base)


def test_ready_unblocked_unassigned_row_is_eligible():
    assert skip_reason(_row(), min_status="Ready", respect_dependencies=True, now=NOW) is None


def test_status_gate_honours_min_status():
    kw = dict(respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Backlog"), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Backlog"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Blocked"), min_status="Backlog", **kw) == "blocked"
    assert skip_reason(_row(status="Done"), min_status="Backlog", **kw) == "status"


def test_open_blocker_blocks_unless_dependencies_are_ignored():
    kw = dict(min_status="Ready", now=NOW)
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=True, **kw) == "blocked"
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=False, **kw) is None


def test_live_session_claim_assignee_epic_and_bot_are_excluded():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(live_session=True), **kw) == "live_session"
    assert skip_reason(_row(claimed=True), **kw) == "claimed"
    assert skip_reason(_row(assignees=("steiner385",)), **kw) == "assigned"
    assert skip_reason(_row(open_children=3), **kw) == "epic_open"
    assert skip_reason(_row(author="renovate[bot]"), **kw) == "bot"
    assert skip_reason(_row(labels=("dependencies",)), **kw) == "bot"
    assert skip_reason(_row(title="Dependency Dashboard"), **kw) == "bot"


def test_held_rows_are_skipped_regardless_of_time():
    """Spec 2026-09-18 §7: an owner_action or decision hold parks the row until a
    human signal releases it — there is no timer to wait out."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(hold_kind="owner_action"), **kw) == "held"
    assert skip_reason(_row(hold_kind="decision"), **kw) == "held"
    # the two auto-exiting kinds are eligible
    assert skip_reason(_row(hold_kind="evidence"), **kw) is None
    assert skip_reason(_row(hold_kind="stronger_model"), **kw) is None


def test_needs_human_label_alone_never_skips():
    """Phase D: the label is not a signal; only the hold is."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(labels=("needs-human",), gh_updated_at=NOW), **kw) is None


def test_held_outranks_the_board_status_it_causes():
    """The reconciler stamps ``Needs human`` on exactly these rows, so a
    status-first check would report them as plain "status" skips and the card
    would lose its held gauge."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Needs human", hold_kind="owner_action"), **kw) == "held"


def test_needs_spec_skips_a_bounced_row_before_the_status_gate():
    """D8: a row that keeps bouncing on the same body is parked as ``needs_spec``
    — checked immediately after the held check and before the status gate, so it
    is reported whatever the board's Status column says."""
    kw = dict(min_status=STATUS_READY, respect_dependencies=True, now=NOW)
    row = _row(bounces=2, status=STATUS_READY)
    assert skip_reason(row, give_up_after_bounces=2, **kw) == "needs_spec"
    assert skip_reason(row, give_up_after_bounces=3, **kw) is None
    assert skip_reason(row, give_up_after_bounces=0, **kw) is None  # 0 = never
    assert "needs_spec" in SKIP_REASONS


def test_rank_counts_needs_spec():
    rows = [_row(ref="o/r#1", number=1, bounces=2), _row(ref="o/r#2", number=2, bounces=0)]
    entries, skipped = rank(rows, min_status=STATUS_READY, respect_dependencies=True,
                            now=NOW, give_up_after_bounces=2)
    assert skipped == {"needs_spec": 1}
    assert [e.number for e in entries] == [2]


def test_rank_carries_hold_kind_onto_entries():
    entries, skipped = rank([_row(hold_kind="evidence"), _row(ref="o/r#2", number=2, hold_kind="owner_action")],
                            min_status="Backlog", respect_dependencies=True, now=NOW)
    assert [(e.ref, e.hold_kind) for e in entries] == [("o/r#1", "evidence")]
    assert skipped == {"held": 1}
    assert "held" in SKIP_REASONS and "needs_human_cooldown" not in SKIP_REASONS


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


def test_rank_key_is_the_one_ordering_and_applies_to_rows_and_entries():
    """``rank`` is not always the last word on ordering: a caller that ranks each
    product separately (the Backlog Runner, whose products have their own reach)
    has to merge, and that merge must sort by exactly this key rather than a
    hand-copied tuple that can drift from it. So the key is exported, and it takes
    a ``QueueRow`` before ranking and a ``QueueEntry`` after it."""
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", score=None, gh_created_at=None)]
    assert [r.ref for r in sorted(rows, key=rank_key)] == [
        "o/r#4", "o/r#3", "o/r#1", "o/r#2", "o/r#5"]

    # The same key over ENTRIES reproduces rank()'s own order — which is what
    # makes a cross-product merge and a single rank() agree.
    entries, _ = rank(rows, now=NOW)
    assert [e.ref for e in sorted(entries, key=rank_key)] == [e.ref for e in entries]

    # An undated row sorts last on its own boolean leg rather than via a sentinel
    # datetime, so the key never compares a naive datetime with an aware one.
    undated = _row(ref="o/r#9", score=5.0, gh_created_at=None)
    # Index 3, not 2: urgency is a leading leg of the key (see the incident tests
    # at the bottom of this file).
    assert rank_key(undated)[3] is True
    assert sorted([undated, rows[0]], key=rank_key)[0].ref == "o/r#1"


def test_to_candidate_is_the_runner_shape_plus_provenance():
    entries, _ = rank([_row()], now=NOW)
    assert to_candidate(entries[0]) == {"ref": "o/r#1", "number": 1, "repo": "o/r", "title": "t",
                                        "body": "", "url": "u", "score": 10.0, "product": "viberpit",
                                        "urgent": False, "hold_kind": None}


def test_to_candidate_carries_urgency_to_the_grouper():
    # The grouper re-orders the pool into a plan, so urgency has to survive the
    # hand-off as DATA — the candidate dict is the only thing it receives.
    entries, _ = rank([_row(labels=("deploy-monitor",))], now=NOW)
    assert to_candidate(entries[0])["urgent"] is True


def test_rank_rejects_unknown_min_status():
    kw = dict(respect_dependencies=True, now=NOW)
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
    kw = dict(respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status=None), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Todo"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status=None), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Todo"), min_status="Ready", **kw) == "status"


def test_every_skip_reason_is_in_the_vocabulary():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW,
             give_up_after_bounces=2)
    cases = {
        "status": _row(status="Done"),
        "blocked": _row(status="Blocked"),
        "live_session": _row(live_session=True),
        "claimed": _row(claimed=True),
        "assigned": _row(assignees=("steiner385",)),
        "epic_open": _row(open_children=1),
        "bot": _row(author="renovate[bot]"),
        "held": _row(hold_kind="owner_action"),
        "needs_spec": _row(bounces=2),
    }
    seen = set()
    for expected, row in cases.items():
        got = skip_reason(row, **kw)
        assert got == expected
        seen.add(got)
    assert seen == set(SKIP_REASONS)


# ── urgency: a reported incident preempts the queue (#16150, #16151) ──────────

def test_a_reported_incident_outranks_everything_regardless_of_score_and_age():
    """The incident the monitor files is unscored and brand new — which under a
    score-then-oldest key is the WORST possible position, not the best.

    Live evidence: cairnea/KinDash#16150 and #16151 reported both admin
    environments down and ranked 328th and 329th of 332, behind three-day-old
    Playwright flakes, for the seven hours the environments stayed broken.
    Urgency is therefore a LEADING leg of the key: no score and no age can put a
    downed environment behind ordinary backlog.
    """
    incident = _row(ref="o/r#900", score=None, labels=("deploy-monitor",),
                    gh_created_at=NOW)  # unscored AND newest: dead last before this
    scored_old = _row(ref="o/r#1", score=9.0, gh_created_at=NOW - timedelta(days=30))
    plain = _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=1))
    assert [r.ref for r in sorted([scored_old, plain, incident], key=rank_key)] == [
        "o/r#900", "o/r#1", "o/r#2"]


def test_urgency_is_case_insensitive_like_the_bot_label_check():
    assert rank_key(_row(labels=("Deploy-Monitor",)))[0] is False


def test_urgency_survives_the_row_to_entry_merge():
    """``rank_key`` must answer the same before and after ranking: the Backlog
    Runner ranks each product separately and re-sorts the MERGED entries. Urgency
    living only on the row would be silently dropped by exactly that merge."""
    rows = [_row(ref="o/r#1", score=9.0, gh_created_at=NOW - timedelta(days=30)),
            _row(ref="o/r#900", score=None, labels=("deploy-monitor",), gh_created_at=NOW)]
    entries, _ = rank(rows, now=NOW)
    assert [e.ref for e in entries] == ["o/r#900", "o/r#1"]
    assert (entries[0].urgent, entries[1].urgent) == (True, False)
    assert [e.ref for e in sorted(entries, key=rank_key)] == [e.ref for e in entries]


def test_urgent_labels_pin_the_deploy_monitor_label():
    # Same pinning rationale as MIN_STATUS_OPTIONS above: the label is OWNED by
    # the automation that files the issue, but importing an automation into the
    # pure ranking core would invert the layering. Pin the coupling here so
    # renaming the automation's LABEL fails this test instead of silently
    # demoting every incident back to the bottom of the queue.
    from server.automations.deploy_monitor import LABEL

    assert LABEL in URGENT_LABELS


def test_ordinary_ranking_is_unchanged_by_the_urgency_leg():
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", score=None, gh_created_at=None)]
    assert [r.ref for r in sorted(rows, key=rank_key)] == [
        "o/r#4", "o/r#3", "o/r#1", "o/r#2", "o/r#5"]
