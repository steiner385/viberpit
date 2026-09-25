from datetime import datetime, timezone
from server.pm.desired_state import (
    ItemFacts, Decision, derive, derive_status,
    STATUS_BACKLOG, STATUS_BLOCKED, STATUS_DONE, STATUS_HELD, STATUS_IN_PROGRESS,
    STATUS_OPTIONS, STATUS_READY)
from server.pm.score import rice_score

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)
T3 = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _f(**over):
    base = dict(ref="o/r#1", issue_state="open", issue_closed_at=None, triage_state="unclassified",
                classified_at=None, reach=None, impact=None, effort=None, confidence=None,
                session_status=None, session_at=None, merged_pr_at=None, open_blocker_refs=(),
                blockers_at=None, initiative_stage=None,
                initiative_stage_at=None, hold_kind=None, hold_set_at=None)
    base.update(over)
    return ItemFacts(**base)


def test_rice_score_formula_and_guards():
    assert rice_score(4, 3, 2, 80) == 480.0
    assert rice_score(4, 3, 0, 80) is None
    assert rice_score(None, 3, 2, 80) is None


def test_status_precedence_done_beats_everything():
    f = _f(merged_pr_at=T3, session_status="in_progress", session_at=T2, open_blocker_refs=("o/r#9",))
    assert derive_status(f) == Decision(STATUS_DONE, T3)
    assert derive_status(_f(issue_state="closed", issue_closed_at=T1)) == Decision(STATUS_DONE, T1)


def test_status_session_signals():
    assert derive_status(_f(session_status="in_progress", session_at=T2)) == Decision(STATUS_IN_PROGRESS, T2)
    assert derive_status(_f(session_status="waiting_question", session_at=T2)) == Decision(STATUS_HELD, T2)


def test_status_hold_blocked_ready_backlog():
    # An owner_action or pending decision hold is the board's "Held"; the
    # hold's own timestamp is the decision (spec 2026-09-18 §7).
    assert derive_status(_f(hold_kind="owner_action", classified_at=T1)) == Decision(STATUS_HELD, T1)
    assert derive_status(_f(hold_kind="decision", classified_at=T1)) == Decision(STATUS_HELD, T1)
    # …and hold_set_at is that timestamp, ahead of classified_at: reject_triage,
    # apply_session_hold and _restore_overlay all write a hold WITHOUT touching
    # classified_at, so on a freshly-proposed item it is NULL and should_push(None, …)
    # is False — the board would never be stamped Held at all (F5).
    assert derive_status(_f(hold_kind="owner_action", classified_at=None,
                            hold_set_at=T2)) == Decision(STATUS_HELD, T2)
    assert derive_status(_f(hold_kind="owner_action", classified_at=T1,
                            hold_set_at=T2)) == Decision(STATUS_HELD, T2)
    # evidence and stronger_model holds never park the card
    assert derive_status(_f(hold_kind="evidence", triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f(hold_kind="stronger_model")) == Decision(STATUS_BACKLOG, None)
    # the removed triage value is inert even if a stale row still carries it
    assert derive_status(_f(triage_state="needs_human", classified_at=T1)) == Decision(STATUS_BACKLOG, None)
    assert derive_status(_f(open_blocker_refs=("o/r#2",), blockers_at=T2)) == Decision(STATUS_BLOCKED, T2)
    assert derive_status(_f(triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f()) == Decision(STATUS_BACKLOG, None)


def test_failed_turn_is_not_a_human_wait():
    from server.pm.desired_state import (FAILED_TURN_SESSION_STATUSES,
                                         HUMAN_WAIT_SESSION_STATUSES, LIVE_SESSION_STATUSES)
    assert "needs_attention" not in HUMAN_WAIT_SESSION_STATUSES
    assert FAILED_TURN_SESSION_STATUSES == frozenset({"needs_attention"})
    assert "needs_attention" in LIVE_SESSION_STATUSES
    # A failed runner turn is a bounce the sweep handles, not a parked card — and
    # not a card the reconciler pushes back to Ready either: the session is still
    # live, so the board says In progress. ``pm.tree.issue_tier`` reads exactly the
    # same union as in-flight, so the two surfaces cannot disagree (F1).
    d = derive_status(_f(session_status="needs_attention", session_at=T2,
                         triage_state="classified", classified_at=T1))
    assert d == Decision(STATUS_IN_PROGRESS, T2)
    assert d.value != STATUS_HELD


def test_held_is_the_only_spelling_the_engine_produces():
    """Spec 2026-09-18 §9: the board value is ``Held``. The fleet rename landed and
    the read alias was dropped, so ``Held`` is the sole spelling everywhere."""
    assert STATUS_HELD == "Held"
    assert STATUS_HELD in STATUS_OPTIONS
    # the old slot: between Blocked and Done, so the board's column order is unchanged
    assert STATUS_OPTIONS.index(STATUS_HELD) == STATUS_OPTIONS.index(STATUS_BLOCKED) + 1


def test_derive_bundles_score_and_stage():
    d = derive(_f(triage_state="classified", classified_at=T1, reach=4, impact=3, effort=2, confidence=80,
                  initiative_stage="build", initiative_stage_at=T2))
    assert d.score == Decision(480.0, T1)
    assert d.stage == Decision("build", T2)
    assert derive(_f()).score == Decision(None, None)
