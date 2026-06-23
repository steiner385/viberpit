import pytest

from server.sessions.status import StatusSignals, derive_status


def S(**kw):
    base = dict(turn_active=False, pending_permission=False,
                pending_question=False, pending_plan=False, turns_completed=0,
                last_turn_outcome="success")
    base.update(kw)
    return StatusSignals(**base)


@pytest.mark.parametrize("sig,expected", [
    (S(),                                                              "idle"),
    (S(turns_completed=1),                                            "done"),
    (S(turn_active=True),                                             "in_progress"),
    (S(turn_active=True, turns_completed=2),                         "in_progress"),
    (S(turn_active=True, pending_permission=True),                  "waiting_input"),
    (S(turn_active=True, pending_question=True),                    "waiting_question"),
    (S(pending_permission=True),                                     "waiting_input"),
    (S(pending_question=True),                                       "waiting_question"),
    (S(turn_active=True, pending_permission=True, pending_plan=True), "ready_review"),
    (S(pending_plan=True),                                           "ready_review"),
    # precedence: plan outranks a plain pending permission
    (S(pending_permission=True, pending_plan=True),                 "ready_review"),
    # new cases: terminal outcomes
    (S(turns_completed=1, last_turn_outcome="success"),               "done"),
    (S(turns_completed=1, last_turn_outcome="complete"),              "complete"),
    (S(turns_completed=1, last_turn_outcome="failed"),                "needs_attention"),
    # an active turn still outranks any terminal outcome
    (S(turn_active=True, turns_completed=1, last_turn_outcome="failed"),   "in_progress"),
    # a pending question still outranks a 'complete' outcome
    (S(turns_completed=1, last_turn_outcome="complete", pending_question=True), "waiting_question"),
    # outcome is ignored before the first completed turn
    (S(turns_completed=0, last_turn_outcome="complete"),             "idle"),
    # condensing outranks an active turn
    (S(turn_active=True, condensing=True),                           "condensing"),
    # condensing=False with active turn → normal in_progress
    (S(turn_active=True, condensing=False),                          "in_progress"),
    # needs-you states outrank condensing
    (S(condensing=True, pending_permission=True),                    "waiting_input"),
    (S(condensing=True, pending_question=True),                      "waiting_question"),
    (S(condensing=True, pending_plan=True),                          "ready_review"),
])
def test_derive_status(sig, expected):
    assert derive_status(sig) == expected


def test_pending_question_is_waiting_question():
    sig = StatusSignals(turn_active=True, pending_permission=False,
                        pending_question=True, pending_plan=False, turns_completed=0)
    assert derive_status(sig) == "waiting_question"


def test_pending_permission_is_waiting_input():
    sig = StatusSignals(turn_active=True, pending_permission=True,
                        pending_question=False, pending_plan=False, turns_completed=0)
    assert derive_status(sig) == "waiting_input"


def test_pending_plan_beats_question():
    sig = StatusSignals(turn_active=True, pending_permission=True,
                        pending_question=True, pending_plan=True, turns_completed=0)
    assert derive_status(sig) == "ready_review"
