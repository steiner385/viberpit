import pytest
from server.mcp_spec import ToolArgError
from server.sessions.completion import NO_PR_RESOLUTIONS, parse_mark_no_pr
from server.sessions.model_request import MODEL_TIERS, parse_request_model


def test_no_pr_resolutions_are_the_tokens_resolution_from_text_already_returns():
    assert NO_PR_RESOLUTIONS == ("not-needed", "cancelled", "awaiting-decision")


def test_parse_mark_no_pr_accepts_the_decision_park():
    """Spec 2026-09-18 §6: the ending a session declares after ``propose_decision``.
    It is a no-PR resolution, not a hand-back — the issue is parked on the proposal,
    and the Backlog Runner reads this token back to settle the dispatch."""
    from server.sessions.completion import AWAITING_DECISION
    assert AWAITING_DECISION in NO_PR_RESOLUTIONS
    assert parse_mark_no_pr({"resolution": AWAITING_DECISION,
                             "note": "stop it or let it finish?"}) == {
        "resolution": "awaiting-decision", "note": "stop it or let it finish?"}


def test_parse_mark_no_pr_names_every_resolution_when_it_refuses_one():
    """The error text is the model's only correction, so it must not silently stop
    listing a token the enum accepts."""
    with pytest.raises(ToolArgError) as e:
        parse_mark_no_pr({"resolution": "parked"})
    for token in NO_PR_RESOLUTIONS:
        assert token in str(e.value)


def test_parse_mark_no_pr_accepts_a_valid_resolution_and_note():
    assert parse_mark_no_pr({"resolution": "cancelled", "note": "user pulled it"}) == {
        "resolution": "cancelled", "note": "user pulled it"}


def test_parse_mark_no_pr_defaults_note_to_none():
    assert parse_mark_no_pr({"resolution": "not-needed"})["note"] is None


@pytest.mark.parametrize("args", [{}, {"resolution": "nope"}, {"resolution": ""},
                                  {"resolution": None}, {"note": "x"}])
def test_parse_mark_no_pr_rejects_a_bad_resolution(args):
    with pytest.raises(ToolArgError):
        parse_mark_no_pr(args)


def test_parse_request_model_accepts_a_tier_and_reason():
    assert parse_request_model({"model": "opus", "reason": "cross-module refactor"}) == {
        "model": "opus", "reason": "cross-module refactor"}


def test_parse_request_model_normalizes_case_and_whitespace():
    assert parse_request_model({"model": " Opus ", "reason": "x"})["model"] == "opus"


@pytest.mark.parametrize("args", [{"model": "opus"}, {"model": "opus", "reason": "  "},
                                  {"reason": "x"}, {"model": "gpt-5", "reason": "x"}])
def test_parse_request_model_rejects_a_missing_reason_or_unknown_tier(args):
    with pytest.raises(ToolArgError):
        parse_request_model(args)


def test_model_tiers_match_the_marker_instruction_rubric():
    assert MODEL_TIERS == ("haiku", "sonnet", "opus", "fable")
