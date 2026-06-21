import pytest

from server.sessions.status import StatusSignals, derive_status


def S(**kw):
    base = dict(turn_active=False, pending_permission=False,
                pending_question=False, pending_plan=False, turns_completed=0)
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
])
def test_derive_status(sig, expected):
    assert derive_status(sig) == expected
