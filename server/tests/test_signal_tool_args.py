import pytest
from server.mcp_spec import ToolArgError
from server.sessions.completion import NO_PR_RESOLUTIONS, parse_mark_no_pr
from server.sessions.model_request import MODEL_TIERS, parse_request_model


def test_no_pr_resolutions_are_the_tokens_resolution_from_text_already_returns():
    assert NO_PR_RESOLUTIONS == ("not-needed", "cancelled")


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
