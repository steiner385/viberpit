from server.sessions.status import StatusSignals, derive_status


def _sig(**kw):
    base = dict(turn_active=False, pending_permission=False, pending_question=False,
                pending_plan=False, turns_completed=0, last_turn_outcome="success")
    base.update(kw)
    return StatusSignals(**base)


def test_retrying_when_active_and_retrying():
    assert derive_status(_sig(turn_active=True, retrying=True)) == "retrying"


def test_active_without_retrying_is_in_progress():
    assert derive_status(_sig(turn_active=True)) == "in_progress"


def test_pending_question_outranks_retrying():
    assert derive_status(_sig(turn_active=True, retrying=True, pending_question=True)) == "waiting_question"
