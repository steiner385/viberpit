"""Codex reads the signalling tools off its `dynamicToolCall` items.

The Codex twin of ``test_translator_signal_tools.py``: since Codex's
``durable_tool_surface`` went True its threads declare ``mark_complete`` & co., so
the translator must read a call to one as the turn's outcome — tool first, the
marker as the permanent fallback — and never render it as a card.
"""
from __future__ import annotations

from typing import Any

import pytest

from viberpit import brand
from server.codex import schema
from server.sessions import events as ev
from server.sessions.completion import COMPLETION_MARKER
from server.sessions.tools.registry import TURN_SIGNAL_NAMES
from server.tests.test_codex_translator import (THREAD, _agent_delta, _fresh, _item_completed,
                                                _item_started, _notify, _turn_completed,
                                                _turn_wire)


def _call(tool: str, arguments: Any = None, *, item_id: str = "it_sig",
          status: str = "inProgress") -> dict[str, Any]:
    return {"type": "dynamicToolCall", "id": item_id, "tool": tool, "status": status,
            "arguments": {} if arguments is None else arguments}


def _signal(translator, tool: str, arguments: Any = None, *,
            item_id: str = "it_sig") -> list[object]:
    """One signal call as the app-server streams it: started, then completed."""
    out = _item_started(translator, _call(tool, arguments, item_id=item_id))
    out += _item_completed(translator, _call(tool, arguments, item_id=item_id,
                                             status="completed"))
    return out


def _end(translator) -> ev.TurnEnd:
    out = _turn_completed(translator)
    assert isinstance(out[-1], ev.TurnEnd)
    return out[-1]


@pytest.mark.parametrize("tool", sorted(TURN_SIGNAL_NAMES))
def test_a_turn_signal_call_renders_no_card(tool: str) -> None:
    translator, _ = _fresh()
    assert _signal(translator, tool) == []


def test_a_non_turn_signal_tool_renders_its_card_under_the_canonical_name() -> None:
    """`set_hold` / `share_artifact` have lasting effects the card is the only
    transcript record of — the same split the Claude translator keeps — and the
    card is named exactly as its Claude twin, which is what the web keys on."""
    translator, _ = _fresh()
    out = _signal(translator, "share_artifact", {"kind": "url", "value": "https://x"})
    assert [type(e) for e in out] == [ev.ToolCall, ev.ToolResult]
    call = out[0]
    assert call.name == brand.MCP_TOOL_PREFIX + "share_artifact"  # type: ignore[attr-defined]
    assert call.kind == ev.tool_kind(call.name)  # type: ignore[attr-defined]


def test_a_dynamic_tool_result_carries_the_handlers_text_and_verdict() -> None:
    """A shared image's served URL lives in the handler's answer; the result
    carries it in the Claude `content` shape so one card reads both engines."""
    translator, _ = _fresh()
    done = {**_call("share_artifact", {"kind": "image", "value": "/x.png"},
                    status="completed"),
            "success": False,
            "contentItems": [{"type": "inputText", "text": '{"error": "nope"}'},
                             {"type": "inputImage", "imageUrl": "data:,"}]}
    result = _item_completed(translator, done)[-1]
    assert isinstance(result, ev.ToolResult)
    assert result.ok is False
    assert result.payload == {"status": "completed",
                              "content": [{"type": "text", "text": '{"error": "nope"}'}]}


def test_mark_complete_completes_the_turn_without_any_marker() -> None:
    translator, _ = _fresh()
    _signal(translator, "mark_complete")
    _agent_delta(translator, "it_a", "All merged. Closing paragraph after the call.")
    assert _end(translator).outcome == "complete"


def test_mark_no_pr_carries_its_resolution_and_note_from_the_tool() -> None:
    translator, _ = _fresh()
    _signal(translator, "mark_no_pr", {"resolution": "not-needed", "note": "already fixed"})
    end = _end(translator)
    assert end.outcome == "complete"
    assert (end.pr_resolution, end.pr_resolution_source, end.pr_resolution_note) == (
        "not-needed", "tool", "already fixed")


def test_hand_back_ends_the_turn_with_its_reason_and_outranks_completion() -> None:
    translator, _ = _fresh()
    _signal(translator, "mark_no_pr", {"resolution": "cancelled"})
    _signal(translator, "hand_back", {"reason": "pick A or B"}, item_id="it_hb")
    end = _end(translator)
    assert (end.outcome, end.hand_back_reason) == ("hand_back", "pick A or B")
    assert (end.pr_resolution, end.pr_resolution_source,
            end.pr_resolution_note) == (None, None, None)


def test_request_model_rides_the_turn_end() -> None:
    translator, _ = _fresh()
    _signal(translator, "request_model", {"model": "haiku", "reason": "trivial bump"})
    assert _end(translator).model_request == {"model": "haiku", "reason": "trivial bump"}


def test_a_malformed_signal_is_dropped_not_fatal() -> None:
    """The handler already refused it to the model; the turn must still close."""
    translator, _ = _fresh()
    _signal(translator, "hand_back", {"reason": "   "})
    end = _end(translator)
    assert (end.outcome, end.hand_back_reason) == ("success", None)


def test_the_marker_still_finishes_a_thread_started_without_the_tools() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", f"done\n{COMPLETION_MARKER}")
    end = _end(translator)
    assert end.outcome == "complete" and end.pr_resolution_source is None


def test_a_signal_for_another_turn_never_closes_the_live_one() -> None:
    """Replayed history (`thread/read`) arrives under its own turn id."""
    translator, _ = _fresh()
    _item_completed(translator, _call("mark_complete", status="completed"),
                    turn_id="tn_old")
    assert _end(translator).outcome == "success"


def test_signals_do_not_leak_into_the_next_turn() -> None:
    translator, _ = _fresh()
    _signal(translator, "mark_complete")
    _end(translator)
    translator.translate(schema.METHOD_TURN_STARTED, _notify(
        schema.METHOD_TURN_STARTED,
        {"threadId": THREAD, "turn": _turn_wire(status="inProgress")}))
    assert _end(translator).outcome == "success"
