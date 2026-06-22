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
    (S(turn_active=True, pending_question=True),                    "waiting_input"),
    (S(pending_permission=True),                                     "waiting_input"),
    (S(pending_question=True),                                       "waiting_input"),
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
    # a pending prompt still outranks a 'complete' outcome
    (S(turns_completed=1, last_turn_outcome="complete", pending_question=True), "waiting_input"),
    # outcome is ignored before the first completed turn
    (S(turns_completed=0, last_turn_outcome="complete"),             "idle"),
])
def test_derive_status(sig, expected):
    assert derive_status(sig) == expected
