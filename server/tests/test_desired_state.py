from datetime import datetime, timezone
from server.pm.desired_state import (
    ItemFacts, Decision, derive, derive_status, STATUS_BACKLOG, STATUS_BLOCKED, STATUS_DONE,
    STATUS_IN_PROGRESS, STATUS_NEEDS_HUMAN, STATUS_READY)
from server.pm.score import rice_score

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)
T3 = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _f(**over):
    base = dict(ref="o/r#1", issue_state="open", issue_closed_at=None, triage_state="unclassified",
                classified_at=None, reach=None, impact=None, effort=None, confidence=None,
                session_status=None, session_at=None, merged_pr_at=None, open_blocker_refs=(),
                blockers_at=None, needs_human_label=False, initiative_stage=None,
                initiative_stage_at=None)
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
    assert derive_status(_f(session_status="waiting_question", session_at=T2)) == Decision(STATUS_NEEDS_HUMAN, T2)


def test_status_needs_human_blocked_ready_backlog():
    assert derive_status(_f(triage_state="needs_human", classified_at=T1)) == Decision(STATUS_NEEDS_HUMAN, T1)
    assert derive_status(_f(needs_human_label=True)) .value == STATUS_NEEDS_HUMAN
    assert derive_status(_f(open_blocker_refs=("o/r#2",), blockers_at=T2)) == Decision(STATUS_BLOCKED, T2)
    assert derive_status(_f(triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f()) == Decision(STATUS_BACKLOG, None)


def test_derive_bundles_score_and_stage():
    d = derive(_f(triage_state="classified", classified_at=T1, reach=4, impact=3, effort=2, confidence=80,
                  initiative_stage="build", initiative_stage_at=T2))
    assert d.score == Decision(480.0, T1)
    assert d.stage == Decision("build", T2)
    assert derive(_f()).score == Decision(None, None)
