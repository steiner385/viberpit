"""Guards: dedupe, cap, flip-flop halt, settled-question drop."""
import pytest

from server.sessions.self_model_guards import SelfModelState, evaluate_guards


def _ev(state, current, requested, cap=3):
    return evaluate_guards(state, current_tier=current, requested_tier=requested, cap=cap)


def test_first_request_is_allowed():
    assert _ev(SelfModelState(), "opus", "haiku") == "auto_ok"


def test_same_tier_is_dropped():
    assert _ev(SelfModelState(), "opus", "opus") == "drop"


def test_request_for_a_tier_a_human_already_ruled_on_is_dropped():
    """Once a human amended a request, the session must not re-ask for it."""
    state = SelfModelState(amended_away={"fable"})
    assert _ev(state, "sonnet", "fable") == "drop"


def test_amendment_does_not_block_a_different_tier():
    state = SelfModelState(amended_away={"fable"})
    assert _ev(state, "sonnet", "haiku") == "auto_ok"


def test_returning_to_a_tier_already_left_forces_a_park():
    """opus -> haiku -> opus is a flip-flop; the second move needs a human."""
    state = SelfModelState(tiers_left={"opus"})
    assert _ev(state, "haiku", "opus") == "force_park"


def test_cap_degrades_to_parking_rather_than_dying():
    state = SelfModelState(auto_applied=3)
    assert _ev(state, "opus", "haiku", cap=3) == "force_park"


def test_under_the_cap_still_auto_applies():
    state = SelfModelState(auto_applied=2)
    assert _ev(state, "opus", "haiku", cap=3) == "auto_ok"


@pytest.mark.parametrize("current,requested", [(None, "haiku"), ("opus", None)])
def test_untiered_sides_are_not_dropped_here(current, requested):
    """Direction is the policy's job; guards must not swallow the request."""
    assert _ev(SelfModelState(), current, requested) == "auto_ok"
