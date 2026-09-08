"""The <!-- ccswitch:model=... --> contract: parse, strip, and prompt injection."""
from server.sessions.completion import COMPLETION_MARKER, strip_markers
from server.sessions.model_request import (
    MODEL_REQUEST_INSTRUCTION,
    parse_model_request,
)


def test_parses_alias_and_reason():
    text = 'Done.\n<!-- ccswitch:model=fable reason="cross-module refactor" -->'
    assert parse_model_request(text) == {"model": "fable", "reason": "cross-module refactor"}


def test_parses_without_reason():
    assert parse_model_request("<!-- ccswitch:model=haiku -->") == {
        "model": "haiku", "reason": None}


def test_parses_pinned_id_and_context_tag():
    assert parse_model_request("<!-- ccswitch:model=claude-opus-5 -->")["model"] == "claude-opus-5"
    assert parse_model_request("<!-- ccswitch:model=fable[1m] -->")["model"] == "fable[1m]"


def test_last_marker_wins():
    text = "<!-- ccswitch:model=haiku -->\nthinking again\n<!-- ccswitch:model=opus -->"
    assert parse_model_request(text)["model"] == "opus"


def test_reason_may_contain_punctuation_but_not_break_out():
    text = '<!-- ccswitch:model=opus reason="it is tricky: races, locks (ugh)" -->'
    assert parse_model_request(text)["reason"] == "it is tricky: races, locks (ugh)"


def test_absent_or_malformed_returns_none():
    for text in ("", "no marker here", "<!-- ccswitch:model= -->",
                 "<!-- ccswitch:model -->", "<!-- ccswitch:complete -->"):
        assert parse_model_request(text) is None, text


def test_non_str_input_returns_none():
    """Fail-open: never raise on non-str input."""
    assert parse_model_request(123) is None
    assert parse_model_request(["x"]) is None


def test_strip_markers_removes_the_model_marker():
    """Load-bearing: an unstripped marker would defeat the trailing-question heuristic."""
    text = 'All set?\n<!-- ccswitch:model=opus reason="x" -->'
    assert strip_markers(text) == "All set?"


def test_strip_markers_still_removes_completion_marker():
    assert strip_markers(f"done\n{COMPLETION_MARKER}") == "done"


def test_instruction_names_every_tier():
    for tier in ("haiku", "sonnet", "opus", "fable"):
        assert tier in MODEL_REQUEST_INSTRUCTION


def test_sdk_factory_appends_the_instruction():
    """With the session-tools surface off, the marker instruction is appended
    directly. With tools mounted (the default), `request_model` replaces it
    instead — never both (task-8, marker-to-tool migration)."""
    from server.sessions.claude_sdk_factory import _build_append_string
    append = _build_append_string()
    assert "request_model" in append
    assert MODEL_REQUEST_INSTRUCTION not in append
