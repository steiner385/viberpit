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
    # hand_back: the agent declared it needs the user. Its OWN status (#2321) — NOT
    # waiting_question, which promises a card the user can tap and a hand-back has none.
    (S(turns_completed=1, last_turn_outcome="hand_back"),             "handed_back"),
    # an active turn still outranks a hand-back from the previous turn
    (S(turn_active=True, turns_completed=1, last_turn_outcome="hand_back"), "in_progress"),
    # a structured card still outranks it (plan > question > permission unchanged)
    (S(turns_completed=1, last_turn_outcome="hand_back", pending_permission=True), "waiting_input"),
    (S(turns_completed=1, last_turn_outcome="hand_back", pending_plan=True), "ready_review"),
    # condensing still outranks it
    (S(turns_completed=1, last_turn_outcome="hand_back", condensing=True, turn_active=True), "condensing"),
    # outcome is ignored before the first completed turn
    (S(turns_completed=0, last_turn_outcome="hand_back"),             "idle"),
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


# ---------------------------------------------------------------------------
# #2321: "Answer needed" must mean "there is a card here to tap"
# ---------------------------------------------------------------------------

def test_hand_back_does_not_borrow_the_question_status():
    """A bare `hand_back` has no structured card, so it must not claim one.

    Reusing waiting_question is what put "Answer needed" on sessions with nothing to
    answer — twice: the 2026-09-08 parked-prose incident (narrowed via the classifier,
    see test_orchestrator_status_name) and again 2026-09-24, when a read-only triage
    session handed back over a harness contradiction and the user opened the card to
    ask "is there an answer needed from me?". The classifier cannot catch this one:
    `hand_back` is a deliberate tool call, not a misfiring prose scan."""
    handed_back = derive_status(S(turns_completed=1, last_turn_outcome="hand_back"))
    real_question = derive_status(S(turns_completed=1, pending_question=True))

    assert handed_back == "handed_back"
    assert real_question == "waiting_question"
    assert handed_back != real_question, "the two states must be distinguishable on the card"


def test_a_real_question_card_outranks_a_prior_hand_back():
    """When both are true the tappable card wins — it is the actionable one."""
    assert derive_status(S(turns_completed=1, last_turn_outcome="hand_back",
                           pending_question=True)) == "waiting_question"
