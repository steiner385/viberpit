"""The error card's recovery kind (D-19): ``failure.error_kind`` and its carriage on the
``error`` event, live and replayed.

The kind is computed once on the server from the event's code and text (reusing
``failure.classify``) and rides the wire as ``kind``, so the web keys the card's
recovery actions off one server-side verdict. Wording for ``context_too_long`` and
``model_unavailable`` is the installed CLI's own (byte-grepped from
``~/.local/share/claude/versions/2.1.318``):

* ``WF = "Prompt is too long"`` — the CLI's ``isApiErrorMessage`` text starts with it;
  ``Prompt is too long \\xB7 automatic compaction failed: …`` is the compaction form;
  ``/^prompt is too long: \\d+ tokens > \\d+ maximum$/`` is the API's body; the error
  table names ``"Input is too long for requested model"`` too ("either status").
* ``There's an issue with the selected model (${n}). It may not exist or you may not
  have access to it.``
"""
from __future__ import annotations

import pytest

from server.codex import schema
from server.codex import translator as ctr
from server.codex.turn_history import decode_event
from server.sessions import events as ev
from server.sessions import failure
from server.sessions.translator import Translator
from server.tests.fakes import fake_sdk as f

CLI_PROMPT_TOO_LONG = "Prompt is too long"
CLI_COMPACTION_FAILED = ("Prompt is too long · automatic compaction failed: "
                         "the request timed out")
API_PROMPT_TOO_LONG = "prompt is too long: 204291 tokens > 200000 maximum"
CLI_MODEL_MISSING = ("There's an issue with the selected model (claude-nope-9). It may not "
                     "exist or you may not have access to it. Run /model to pick a "
                     "different model.")


@pytest.mark.parametrize("code,message,kind", [
    # Claude: the CLI's own context-limit sentences, on a 400 or a 413.
    ("api_400", CLI_PROMPT_TOO_LONG, "context_too_long"),
    ("api_400", CLI_COMPACTION_FAILED, "context_too_long"),
    ("api_400", API_PROMPT_TOO_LONG, "context_too_long"),
    ("api_413", CLI_PROMPT_TOO_LONG, "context_too_long"),
    ("api_400", "Input is too long for requested model.", "context_too_long"),
    # The API body embedded in the CLI's "API Error: 400 {...}" spelling.
    ("api_400", 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
                f'"message":"{API_PROMPT_TOO_LONG}"}}}}', "context_too_long"),
    # An error result with no status whose text IS the CLI sentence.
    ("agent_error", CLI_PROMPT_TOO_LONG, "context_too_long"),
    # The CLI's own rule: a case-insensitive substring, anywhere in the text.
    ("api_400", 'API Error: 413 {"error":{"message":"Input is too long for requested '
                'model."}}', "context_too_long"),
    ("api_413", "Request failed: PROMPT IS TOO LONG", "context_too_long"),
    # Only on the codes an API refusal arrives with.
    ("api_429", "prompt is too long", "rate_limit"),
    ("execution_error", "prompt is too long", "other"),
    ("api_400", "messages.3.content: text is too long", "other"),
    ("api_400", "The model API returned an error (HTTP 400).", "other"),
    ("driver_error", "RuntimeError: prompt is too long somewhere", "other"),
    # Rate limit / overload come from classify().
    ("api_429", "rate limited", "rate_limit"),
    ("api_529", "Anthropic is overloaded (HTTP 529).", "overloaded"),
    ("api_500", "x", "overloaded"),
    ("agent_error", "API Error: Overloaded", "overloaded"),
    # Model unavailable: a 404 or the CLI's sentence.
    ("api_404", "The model API returned an error (HTTP 404).", "model_unavailable"),
    ("agent_error", CLI_MODEL_MISSING, "model_unavailable"),
    # Everything else is "other" — auth failures included.
    ("api_401", "invalid authentication credentials", "other"),
    ("api_403", "x", "other"),
    ("execution_error", "The agent stopped on an execution error.", "other"),
    ("max_turns", "The agent reached the maximum number of turns.", "other"),
    ("", "", "other"),
    (None, "", "other"),
    # Codex categories.
    ("contextWindowExceeded", "the context window is full", "context_too_long"),
    ("rateLimitExceeded", "slow down", "rate_limit"),
    ("usageLimitExceeded", "the model refused", "rate_limit"),
    ("sessionBudgetExceeded", "x", "rate_limit"),
    ("serverOverloaded", "x", "overloaded"),
    ("unauthorized", "x", "other"),
    ("badRequest", "x", "other"),
    ("codex_turn_error", "x", "other"),
    ("codex_thread_error", "x", "other"),
])
def test_error_kind_maps_codes_and_cli_wording(code, message, kind):
    assert failure.error_kind(code, message) == kind


def test_the_token_count_in_a_context_error_is_never_read_as_a_429():
    """``classify`` infers a rate limit from a bare "429" in the text — the API's
    "204291 tokens" carries one. The context check must win."""
    assert failure.classify("api_400", API_PROMPT_TOO_LONG).kind == "rate_limit"
    assert failure.error_kind("api_400", API_PROMPT_TOO_LONG) == "context_too_long"


def test_every_kind_is_in_the_published_vocabulary():
    assert set(failure.ERROR_KINDS) == {"rate_limit", "overloaded", "context_too_long",
                                        "model_unavailable", "other"}


def test_codex_recoverable_kinds_are_failure_kinds_the_driver_reads():
    """One map: the Codex driver's recovery signal reads failure.CODEX_RECOVERABLE_KINDS."""
    import server.codex.driver as cdrv
    assert not hasattr(cdrv, "_RECOVERABLE_ERROR_KINDS")
    assert failure.CODEX_RECOVERABLE_KINDS["rateLimitExceeded"] == "rate_limit"
    assert failure.CODEX_RECOVERABLE_KINDS["unauthorized"] == "auth_401"
    assert "contextWindowExceeded" not in failure.CODEX_RECOVERABLE_KINDS, \
        "a context overflow is not recoverable by rotating accounts"


# ── carriage on the event ────────────────────────────────────────────────────


def test_error_event_wire_carries_the_kind():
    e = ev.ErrorEvent("api_429", "rate limited", turn_id="t1")
    assert e.kind == "rate_limit"
    assert e.to_wire()["kind"] == "rate_limit"


def test_kind_is_computed_from_the_raw_message_when_it_is_humanized():
    e = ev.ErrorEvent(code="api_400", message="Something friendlier",
                      raw_message=CLI_PROMPT_TOO_LONG)
    assert e.to_wire()["kind"] == "context_too_long"


def test_positional_construction_still_works_and_kind_defaults_other():
    e = ev.ErrorEvent("live_stream_failed", "The live session stream stopped.", turn_id=None)
    assert e.to_wire()["kind"] == "other"


def test_claude_translator_error_result_carries_context_too_long():
    tr = Translator()
    out: list[object] = []
    for m in [f.stream_start(uuid="t1"),
              f.result(uuid="t1", subtype="success", is_error=True, api_error_status=400,
                       result=CLI_PROMPT_TOO_LONG)]:
        out += tr.feed(m)
    err = next(e for e in out if isinstance(e, ev.ErrorEvent))
    assert err.code == "api_400"
    assert err.to_wire()["kind"] == "context_too_long"


def _codex_translator():
    t = ctr.CodexEventTranslator(thread_id="th_1")
    t.translate(schema.METHOD_THREAD_STARTED, schema.parse_server_notification(
        schema.METHOD_THREAD_STARTED, {"thread": {
            "id": "th_1", "cwd": "/w", "status": {"type": "active"},
            "modelProvider": "openai", "createdAt": 1, "updatedAt": 2, "turns": [],
            "model": "gpt-5-codex"}}))
    t.translate(schema.METHOD_TURN_STARTED, schema.parse_server_notification(
        schema.METHOD_TURN_STARTED, {"threadId": "th_1", "turn": {
            "id": "tn_1", "status": "inProgress", "items": []}}))
    return t


def test_codex_turn_error_context_window_exceeded_carries_the_kind():
    t = _codex_translator()
    out = t.translate(schema.METHOD_TURN_COMPLETED, schema.parse_server_notification(
        schema.METHOD_TURN_COMPLETED, {"threadId": "th_1", "turn": {
            "id": "tn_1", "status": "failed", "items": [],
            "error": {"message": "the context window is full",
                      "codexErrorInfo": "contextWindowExceeded"}}}))
    err = next(e for e in out if isinstance(e, ev.ErrorEvent))
    assert err.to_wire()["kind"] == "context_too_long"


def test_codex_replay_keeps_a_persisted_kind():
    wire = ev.ErrorEvent("rateLimitExceeded", "slow down", turn_id="tn_1", ts=1.0).to_wire()
    decoded = decode_event(wire)
    assert isinstance(decoded, ev.ErrorEvent)
    assert decoded.to_wire() == wire
    assert decoded.to_wire()["kind"] == "rate_limit"


def test_codex_replay_of_a_record_written_before_kind_existed_computes_it():
    """Turn history written by an older build has no ``kind``: decoding derives it from
    the persisted code, so a replayed card offers the same actions as the live one."""
    old = {"type": "error", "code": "contextWindowExceeded", "message": "full",
           "resetsAt": None, "turnId": "tn_1", "ts": 1.0}
    decoded = decode_event(old)
    assert decoded.to_wire()["kind"] == "context_too_long"
