from server.sessions.status import StatusSignals, derive_status


def _sig(**kw):
    base = dict(turn_active=False, pending_permission=False, pending_question=False,
                pending_plan=False, turns_completed=0)
    base.update(kw)
    return StatusSignals(**base)


def test_condensing_outranks_active_turn():
    assert derive_status(_sig(turn_active=True, condensing=True)) == "condensing"


def test_condensing_yields_only_when_set():
    assert derive_status(_sig(turn_active=True, condensing=False)) == "in_progress"


def test_needs_you_states_outrank_condensing():
    assert derive_status(_sig(condensing=True, pending_permission=True)) == "waiting_input"
    assert derive_status(_sig(condensing=True, pending_question=True)) == "waiting_question"
    assert derive_status(_sig(condensing=True, pending_plan=True)) == "ready_review"
