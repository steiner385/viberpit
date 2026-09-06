"""Codex app-server notifications → the normalized ccswitch event vocabulary.

`server/codex/translator.py` is pure and synchronous, so every rule it enforces
is pinned here by feeding it real `schema.parse_server_notification` DTOs and
asserting on the exact event list that comes back. Two of those rules are
policy rather than preference and get their own explicit tests: raw chain of
thought (`item/reasoning/textDelta`) must never produce an event, and an
unmodelled item type must be counted and dropped rather than guessed at.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.codex import schema, translator as tr
from server.sessions import events as ev
from server.sessions.completion import COMPLETION_MARKER, NO_PR_MARKER

TRANSCRIPT_ROOT = (
    Path(__file__).parent / "fixtures" / "codex_app_server" / "transcripts"
)

THREAD = "th_1"
TURN = "tn_1"


# ---------------------------------------------------------------------------
# Wire builders — every payload goes through the real parser first.
# ---------------------------------------------------------------------------


def _thread_wire(
    *, thread_id: str = THREAD, model: str | None = "gpt-5-codex", status: str = "active"
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": thread_id,
        "cwd": "/w",
        "status": {"type": status},
        "modelProvider": "openai",
        "createdAt": 1,
        "updatedAt": 2,
        "turns": [],
    }
    if model is not None:
        body["model"] = model
    return body


def _turn_wire(
    *,
    turn_id: str = TURN,
    status: str = "completed",
    items: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"id": turn_id, "status": status, "items": items or []}
    if error is not None:
        body["error"] = error
    return body


def _notify(method: str, params: Any) -> schema.ServerNotificationParams:
    return schema.parse_server_notification(method, params)


def _fresh(*, in_turn: bool = True) -> tuple[tr.CodexEventTranslator, list[object]]:
    """A translator with a thread and (by default) an in-flight turn."""
    translator = tr.CodexEventTranslator(thread_id=THREAD)
    emitted: list[object] = []
    emitted += translator.translate(
        schema.METHOD_THREAD_STARTED,
        _notify(schema.METHOD_THREAD_STARTED, {"thread": _thread_wire()}),
    )
    if in_turn:
        emitted += translator.translate(
            schema.METHOD_TURN_STARTED,
            _notify(
                schema.METHOD_TURN_STARTED,
                {"threadId": THREAD, "turn": _turn_wire(status="inProgress")},
            ),
        )
    return translator, emitted


def _item_started(
    translator: tr.CodexEventTranslator,
    item: dict[str, Any],
    *,
    turn_id: str = TURN,
) -> list[object]:
    return translator.translate(
        schema.METHOD_ITEM_STARTED,
        _notify(
            schema.METHOD_ITEM_STARTED,
            {
                "threadId": THREAD,
                "turnId": turn_id,
                "item": item,
                "startedAtMs": 1_000,
            },
        ),
    )


def _item_completed(
    translator: tr.CodexEventTranslator,
    item: dict[str, Any],
    *,
    turn_id: str = TURN,
) -> list[object]:
    return translator.translate(
        schema.METHOD_ITEM_COMPLETED,
        _notify(
            schema.METHOD_ITEM_COMPLETED,
            {
                "threadId": THREAD,
                "turnId": turn_id,
                "item": item,
                "completedAtMs": 2_000,
            },
        ),
    )


def _agent_delta(
    translator: tr.CodexEventTranslator,
    item_id: str,
    delta: str,
    *,
    turn_id: str = TURN,
) -> list[object]:
    return translator.translate(
        schema.METHOD_ITEM_AGENT_MESSAGE_DELTA,
        _notify(
            schema.METHOD_ITEM_AGENT_MESSAGE_DELTA,
            {
                "threadId": THREAD,
                "turnId": turn_id,
                "itemId": item_id,
                "delta": delta,
            },
        ),
    )


def _turn_completed(
    translator: tr.CodexEventTranslator,
    *,
    status: str = "completed",
    error: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> list[object]:
    return translator.translate(
        schema.METHOD_TURN_COMPLETED,
        _notify(
            schema.METHOD_TURN_COMPLETED,
            {
                "threadId": THREAD,
                "turn": _turn_wire(status=status, error=error, items=items),
            },
        ),
    )


# ---------------------------------------------------------------------------
# 1. Delta / final dedupe
# ---------------------------------------------------------------------------


def test_agent_message_deltas_stream_and_the_final_item_is_suppressed() -> None:
    translator, _ = _fresh()
    first = _agent_delta(translator, "it_a", "Hello ")
    second = _agent_delta(translator, "it_a", "world")
    assert [type(e) for e in first + second] == [ev.TextDelta, ev.TextDelta]
    assert [e.text for e in first + second] == ["Hello ", "world"]  # type: ignore[attr-defined]
    assert all(e.turn_id == TURN for e in first + second)  # type: ignore[attr-defined]

    final = _item_completed(
        translator, {"type": "agentMessage", "id": "it_a", "text": "Hello world"}
    )
    assert final == []
    assert translator.counters.suppressed_duplicate_texts == 1


def test_agent_message_completion_without_deltas_emits_the_full_text_once() -> None:
    translator, _ = _fresh()
    out = _item_completed(
        translator, {"type": "agentMessage", "id": "it_a", "text": "Only final"}
    )
    assert [type(e) for e in out] == [ev.TextDelta]
    assert out[0].text == "Only final"  # type: ignore[attr-defined]
    assert translator.counters.suppressed_duplicate_texts == 0
    # A repeat of the same completed item must not double-render.
    assert (
        _item_completed(
            translator, {"type": "agentMessage", "id": "it_a", "text": "Only final"}
        )
        == []
    )
    assert translator.counters.suppressed_duplicate_texts == 1


def test_reasoning_summary_deltas_stream_and_the_final_item_is_suppressed() -> None:
    translator, _ = _fresh()
    out = translator.translate(
        schema.METHOD_ITEM_REASONING_SUMMARY_TEXT_DELTA,
        _notify(
            schema.METHOD_ITEM_REASONING_SUMMARY_TEXT_DELTA,
            {
                "threadId": THREAD,
                "turnId": TURN,
                "itemId": "it_r",
                "delta": "Planning",
                "summaryIndex": 0,
            },
        ),
    )
    assert [type(e) for e in out] == [ev.ThinkingDelta]
    assert out[0].text == "Planning"  # type: ignore[attr-defined]

    assert (
        _item_completed(
            translator,
            {"type": "reasoning", "id": "it_r", "summary": ["Planning the edit"]},
        )
        == []
    )
    assert translator.counters.suppressed_duplicate_texts == 1


def test_reasoning_completion_without_deltas_emits_the_joined_summary() -> None:
    translator, _ = _fresh()
    out = _item_completed(
        translator,
        {"type": "reasoning", "id": "it_r", "summary": ["**Step**", "Do the thing"]},
    )
    assert [type(e) for e in out] == [ev.ThinkingDelta]
    assert out[0].text == "**Step**\n\nDo the thing"  # type: ignore[attr-defined]


def test_reasoning_completion_with_no_summary_emits_nothing() -> None:
    translator, _ = _fresh()
    assert _item_completed(translator, {"type": "reasoning", "id": "it_r"}) == []


# ---------------------------------------------------------------------------
# 3. Raw chain of thought is a privacy contract, not a preference
# ---------------------------------------------------------------------------


def test_raw_reasoning_text_delta_is_refused_and_never_translated() -> None:
    translator, _ = _fresh()
    raw = {
        "threadId": THREAD,
        "turnId": TURN,
        "itemId": "it_r",
        "delta": "secret chain of thought",
    }
    # The parser itself refuses the method — it is declared, never supported.
    with pytest.raises(schema.CodexSchemaError):
        _notify("item/reasoning/textDelta", raw)
    # And the translator refuses it even when a caller hands the params in.
    assert "item/reasoning/textDelta" not in tr.TRANSLATED_NOTIFICATIONS
    params = _notify(
        schema.METHOD_ITEM_REASONING_SUMMARY_TEXT_DELTA, {**raw, "summaryIndex": 0}
    )
    assert translator.translate("item/reasoning/textDelta", params) == []
    assert translator.counters.unknown_notifications == 1


# ---------------------------------------------------------------------------
# 2. Item → tool mapping, ordering, correlation
# ---------------------------------------------------------------------------


ITEM_CASES: list[tuple[dict[str, Any], str, ev.ToolKind, dict[str, Any]]] = [
    (
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "ls -la",
            "cwd": "/w",
            "status": "inProgress",
        },
        "Bash",
        ev.ToolKind.BASH,
        {"command": "ls -la", "cwd": "/w"},
    ),
    (
        {
            "type": "fileChange",
            "id": "it_f",
            "status": "inProgress",
            "changes": [{"path": "/w/a.py", "kind": {"type": "update"}}],
        },
        "Edit",
        ev.ToolKind.EDIT,
        {"changes": [{"path": "/w/a.py", "kind": "update"}]},
    ),
    (
        {
            "type": "mcpToolCall",
            "id": "it_m",
            "server": "linear",
            "tool": "list_issues",
            "status": "inProgress",
            "arguments": {"limit": 5},
        },
        "mcp__linear__list_issues",
        ev.ToolKind.MCP,
        {"limit": 5},
    ),
    (
        {"type": "webSearch", "id": "it_w", "query": "codex app-server"},
        "WebSearch",
        ev.ToolKind.WEB,
        {"query": "codex app-server"},
    ),
    (
        {"type": "plan", "id": "it_p", "text": "1. read 2. write"},
        "Plan",
        ev.ToolKind.TODO,
        {"text": "1. read 2. write"},
    ),
    (
        {
            "type": "subAgentActivity",
            "id": "it_s",
            "agentPath": "agents/reviewer.md",
            "agentThreadId": "th_sub_secret",
            "kind": "started",
        },
        "Task",
        ev.ToolKind.TASK,
        {
            "agent": "agents/reviewer.md",
            "kind": "started",
            "subagent_type": "agents/reviewer.md",
            "description": "started",
        },
    ),
    (
        {
            "type": "collabAgentToolCall",
            "id": "it_k",
            "tool": "ask",
            "status": "inProgress",
            "model": "gpt-5",
            "senderThreadId": "th_parent_secret",
            "receiverThreadIds": ["th_child_secret"],
            "agentsStates": {},
        },
        "Task",
        ev.ToolKind.TASK,
        {
            "tool": "ask",
            "model": "gpt-5",
            "subagent_type": "ask",
            "description": "gpt-5",
        },
    ),
    (
        {
            "type": "dynamicToolCall",
            "id": "it_d",
            "tool": "custom_tool",
            "status": "inProgress",
            "arguments": {"x": 1},
        },
        "custom_tool",
        ev.ToolKind.UNKNOWN,
        {"x": 1},
    ),
    (
        {"type": "functionCallOutput", "id": "it_o", "name": "apply_patch"},
        "apply_patch",
        ev.ToolKind.UNKNOWN,
        {},
    ),
    (
        {"type": "imageView", "id": "it_i", "path": "/w/shot.png"},
        "Read",
        ev.ToolKind.READ,
        {"file_path": "/w/shot.png"},
    ),
]


@pytest.mark.parametrize(
    ("item", "name", "kind", "tool_input"),
    ITEM_CASES,
    ids=[case[0]["type"] for case in ITEM_CASES],
)
def test_item_started_maps_to_the_contracted_tool_call(
    item: dict[str, Any], name: str, kind: ev.ToolKind, tool_input: dict[str, Any]
) -> None:
    translator, _ = _fresh()
    out = _item_started(translator, item)
    assert [type(e) for e in out] == [ev.ToolCall]
    call = out[0]
    assert isinstance(call, ev.ToolCall)
    assert (call.name, call.kind, call.input) == (name, kind, tool_input)
    assert call.tool_id == item["id"]
    assert call.turn_id == TURN


def test_sub_agent_activity_never_carries_the_native_agent_thread_id() -> None:
    translator, _ = _fresh()
    (call,) = _item_started(
        translator,
        {
            "type": "subAgentActivity",
            "id": "it_s",
            "agentPath": "agents/reviewer.md",
            "agentThreadId": "th_sub_secret",
            "kind": "started",
        },
    )
    assert isinstance(call, ev.ToolCall)
    assert "th_sub_secret" not in json.dumps(call.input)


def test_collab_agent_tool_call_never_carries_a_native_thread_id() -> None:
    """#1274 added real sender/receiver identity to the parsed item; the wire

    `ToolCall.input` — a generic, client-rendered dict — must still never
    carry it, matching the same rule `subAgentActivity` already follows.
    """
    translator, _ = _fresh()
    (call,) = _item_started(
        translator,
        {
            "type": "collabAgentToolCall",
            "id": "it_k",
            "tool": "spawnAgent",
            "status": "inProgress",
            "senderThreadId": "th_parent_secret",
            "receiverThreadIds": ["th_child_secret"],
            "agentsStates": {
                "th_child_secret": {"status": "running", "message": "secret note"}
            },
        },
    )
    assert isinstance(call, ev.ToolCall)
    dumped = json.dumps(call.input)
    assert "th_parent_secret" not in dumped
    assert "th_child_secret" not in dumped
    assert "secret note" not in dumped


def test_collab_agent_tool_call_result_counts_agent_states_never_leaks_them() -> None:
    """`agentsStates` becomes an observability counter only (#1274) — the

    counter is a plain int, and the result payload stays as bare as it was
    before this item type carried any collaborator identity.
    """
    translator, _ = _fresh()
    _item_started(
        translator,
        {
            "type": "collabAgentToolCall",
            "id": "it_k",
            "tool": "wait",
            "status": "inProgress",
            "senderThreadId": "th_parent_secret",
            "receiverThreadIds": ["th_child_secret"],
            "agentsStates": {},
        },
    )
    (result,) = _item_completed(
        translator,
        {
            "type": "collabAgentToolCall",
            "id": "it_k",
            "tool": "wait",
            "status": "completed",
            "senderThreadId": "th_parent_secret",
            "receiverThreadIds": ["th_child_secret"],
            "agentsStates": {
                "th_child_secret": {"status": "completed", "message": "secret note"}
            },
        },
    )
    assert isinstance(result, ev.ToolResult)
    assert result.payload == {"status": "completed"}
    assert "th_child_secret" not in json.dumps(result.payload)
    assert "secret note" not in json.dumps(result.payload)
    assert translator.counters.collab_agent_states_seen == 1


def test_completed_item_pairs_a_result_with_the_call_that_started_it() -> None:
    translator, _ = _fresh()
    started = _item_started(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "true",
            "cwd": "/w",
            "status": "inProgress",
        },
    )
    finished = _item_completed(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "true",
            "cwd": "/w",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "ok\n",
            "durationMs": 12,
        },
    )
    assert [type(e) for e in finished] == [ev.ToolResult]
    call, result = started[0], finished[0]
    assert isinstance(call, ev.ToolCall)
    assert isinstance(result, ev.ToolResult)
    assert result.tool_id == call.tool_id == "it_c"
    assert result.ok is True
    assert result.payload == {"exitCode": 0, "output": "ok\n", "durationMs": 12}


def test_completed_item_never_seen_started_emits_the_call_then_the_result() -> None:
    """The replay path: `thread/read` hands back finished items only."""
    translator, _ = _fresh()
    out = _item_completed(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "false",
            "cwd": "/w",
            "status": "failed",
            "exitCode": 1,
            "aggregatedOutput": "boom",
            "durationMs": 3,
        },
    )
    assert [type(e) for e in out] == [ev.ToolCall, ev.ToolResult]
    assert out[1].ok is False  # type: ignore[attr-defined]
    assert out[0].tool_id == out[1].tool_id == "it_c"  # type: ignore[attr-defined]


def test_command_without_an_exit_code_falls_back_to_its_status() -> None:
    translator, _ = _fresh()
    declined = _item_completed(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "rm -rf /",
            "cwd": "/w",
            "status": "declined",
        },
    )
    assert declined[-1].ok is False  # type: ignore[attr-defined]


def test_items_for_another_turn_keep_their_own_turn_id() -> None:
    translator, _ = _fresh()
    assert translator.turn_id == TURN
    (call,) = _item_started(
        translator,
        {
            "type": "commandExecution",
            "id": "it_x",
            "command": "true",
            "cwd": "/w",
            "status": "inProgress",
        },
        turn_id="tn_other",
    )
    assert isinstance(call, ev.ToolCall)
    assert call.turn_id == "tn_other"
    assert translator.turn_id == TURN


def test_user_message_replays_once_as_a_user_event() -> None:
    translator, _ = _fresh()
    out = _item_completed(
        translator,
        {
            "type": "userMessage",
            "id": "it_u",
            "content": [{"type": "text", "text": "do the thing"}],
        },
    )
    assert [type(e) for e in out] == [ev.UserMessage]
    assert out[0].text == "do the thing"  # type: ignore[attr-defined]
    assert out[0].turn_id == TURN  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_command_output_is_capped_at_the_module_constant() -> None:
    translator, _ = _fresh()
    huge = "x" * (tr.MAX_TOOL_OUTPUT_CHARS + 5_000)
    out = _item_completed(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "cat big",
            "cwd": "/w",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": huge,
        },
    )
    payload = out[-1].payload  # type: ignore[attr-defined]
    assert len(payload["output"]) <= tr.MAX_TOOL_OUTPUT_CHARS + len(
        tr.OUTPUT_TRUNCATION_MARKER
    )
    assert payload["output"].endswith(tr.OUTPUT_TRUNCATION_MARKER)
    assert translator.counters.truncated_outputs == 1


def test_output_deltas_aggregate_when_the_item_carries_no_aggregated_output() -> None:
    translator, _ = _fresh()
    _item_started(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "echo hi",
            "cwd": "/w",
            "status": "inProgress",
        },
    )
    for chunk in ("hi", "\n"):
        assert (
            translator.translate(
                schema.METHOD_ITEM_COMMAND_EXECUTION_OUTPUT_DELTA,
                _notify(
                    schema.METHOD_ITEM_COMMAND_EXECUTION_OUTPUT_DELTA,
                    {
                        "threadId": THREAD,
                        "turnId": TURN,
                        "itemId": "it_c",
                        "delta": chunk,
                    },
                ),
            )
            == []
        )
    out = _item_completed(
        translator,
        {
            "type": "commandExecution",
            "id": "it_c",
            "command": "echo hi",
            "cwd": "/w",
            "status": "completed",
            "exitCode": 0,
        },
    )
    assert out[-1].payload["output"] == "hi\n"  # type: ignore[attr-defined]
    assert translator.counters.orphan_deltas == 0


def test_a_delta_for_an_item_that_never_started_counts_as_an_orphan() -> None:
    translator, _ = _fresh()
    assert (
        translator.translate(
            schema.METHOD_ITEM_COMMAND_EXECUTION_OUTPUT_DELTA,
            _notify(
                schema.METHOD_ITEM_COMMAND_EXECUTION_OUTPUT_DELTA,
                {
                    "threadId": THREAD,
                    "turnId": TURN,
                    "itemId": "it_ghost",
                    "delta": "out",
                },
            ),
        )
        == []
    )
    assert translator.counters.orphan_deltas == 1


# ---------------------------------------------------------------------------
# 4. Fail closed on anything unmodelled
# ---------------------------------------------------------------------------


def test_unknown_item_type_is_counted_and_produces_no_event() -> None:
    translator, _ = _fresh()
    unknown = {"type": "quantumThing", "id": "it_q"}
    assert _item_started(translator, unknown) == []
    assert _item_completed(translator, unknown) == []
    assert translator.counters.unknown_items == 2


def test_unknown_notification_method_is_counted_and_produces_no_event() -> None:
    translator, _ = _fresh()
    params = _notify(
        schema.METHOD_THREAD_STATUS_CHANGED,
        {"threadId": THREAD, "status": {"type": "idle"}},
    )
    assert translator.translate("thread/queue/changed", params) == []
    assert translator.counters.unknown_notifications == 1


def test_params_of_the_wrong_type_are_counted_not_guessed_at() -> None:
    translator, _ = _fresh()
    wrong = _notify(
        schema.METHOD_THREAD_STATUS_CHANGED,
        {"threadId": THREAD, "status": {"type": "idle"}},
    )
    assert translator.translate(schema.METHOD_TURN_COMPLETED, wrong) == []
    assert translator.counters.mismatched_params == 1


def test_every_translated_notification_is_a_supported_one() -> None:
    assert tr.TRANSLATED_NOTIFICATIONS <= schema.SUPPORTED_SERVER_NOTIFICATIONS


# ---------------------------------------------------------------------------
# 5 + 6. turn/completed: outcome, error ordering, completion markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("completed", "success"), ("interrupted", "interrupted"), ("failed", "error")],
)
def test_turn_completed_maps_status_to_outcome(status: str, outcome: str) -> None:
    translator, _ = _fresh()
    out = _turn_completed(translator, status=status)
    assert [type(e) for e in out] == [ev.TurnEnd]
    end = out[0]
    assert isinstance(end, ev.TurnEnd)
    assert (end.turn_id, end.outcome) == (TURN, outcome)
    assert end.model_request is None
    assert translator.turn_id is None


def test_turn_error_emits_an_error_event_before_the_turn_end() -> None:
    translator, _ = _fresh()
    out = _turn_completed(
        translator,
        status="failed",
        error={"message": "the model refused", "codexErrorInfo": "usageLimitExceeded"},
    )
    assert [type(e) for e in out] == [ev.ErrorEvent, ev.TurnEnd]
    err = out[0]
    assert isinstance(err, ev.ErrorEvent)
    assert err.turn_id == TURN
    assert err.code == "usageLimitExceeded"
    assert err.message == "the model refused"


def test_completion_marker_upgrades_the_outcome_to_complete() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", f"all done\n{COMPLETION_MARKER}")
    out = _turn_completed(translator)
    assert out[0].outcome == "complete"  # type: ignore[attr-defined]
    assert out[0].pr_resolution is None  # type: ignore[attr-defined]


def test_no_pr_marker_sets_the_pr_resolution() -> None:
    translator, _ = _fresh()
    _item_completed(
        translator,
        {
            "type": "agentMessage",
            "id": "it_a",
            "text": f"nothing to change\n{NO_PR_MARKER}",
        },
    )
    out = _turn_completed(translator)
    assert out[0].outcome == "complete"  # type: ignore[attr-defined]
    assert out[0].pr_resolution == "not-needed"  # type: ignore[attr-defined]


def test_a_marker_never_upgrades_a_failed_turn() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", COMPLETION_MARKER)
    out = _turn_completed(translator, status="failed")
    assert out[-1].outcome == "error"  # type: ignore[attr-defined]


def test_turn_text_does_not_leak_across_turns() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", COMPLETION_MARKER)
    _turn_completed(translator)
    translator.translate(
        schema.METHOD_TURN_STARTED,
        _notify(
            schema.METHOD_TURN_STARTED,
            {"threadId": THREAD, "turn": _turn_wire(turn_id="tn_2", status="inProgress")},
        ),
    )
    out = translator.translate(
        schema.METHOD_TURN_COMPLETED,
        _notify(
            schema.METHOD_TURN_COMPLETED,
            {"threadId": THREAD, "turn": _turn_wire(turn_id="tn_2")},
        ),
    )
    assert out[-1].outcome == "success"  # type: ignore[attr-defined]


def test_turn_started_opens_the_turn() -> None:
    translator, emitted = _fresh()
    assert [type(e) for e in emitted] == [ev.TurnStart]
    assert emitted[-1].turn_id == TURN  # type: ignore[attr-defined]
    assert translator.turn_id == TURN


def test_thread_started_records_the_thread_but_emits_no_session_meta() -> None:
    """`SessionMeta` has exactly one writer, and it is not this notification.

    The driver only learns its thread id from the `thread/start` RESPONSE, so a
    notification emitted before that response is parsed has no registered owner
    and never reaches a translator; a RESUMED thread never sends it at all. The
    driver mints `SessionMeta` from the response, and `CodexThreadReplay` mints
    it from `thread/read` — announcing it here as well would let one session
    report two different models.
    """
    translator = tr.CodexEventTranslator()
    out = translator.translate(
        schema.METHOD_THREAD_STARTED,
        _notify(schema.METHOD_THREAD_STARTED, {"thread": _thread_wire()}),
    )
    assert out == []
    assert translator.thread_id == THREAD
    assert translator.model == "gpt-5-codex"


def test_turn_completed_does_not_replay_the_items_that_already_streamed() -> None:
    translator, _ = _fresh()
    out = _turn_completed(
        translator,
        items=[{"type": "agentMessage", "id": "it_a", "text": "streamed already"}],
    )
    assert [type(e) for e in out] == [ev.TurnEnd]


# ---------------------------------------------------------------------------
# 7. Token usage
# ---------------------------------------------------------------------------


def _usage_wire(*, window: int | None, total: int = 4_000) -> dict[str, Any]:
    breakdown = {
        "inputTokens": 3_000,
        "cachedInputTokens": 500,
        "outputTokens": 1_000,
        "reasoningOutputTokens": 200,
        "totalTokens": total,
        "cacheWriteInputTokens": 42,
    }
    usage: dict[str, Any] = {"total": dict(breakdown), "last": dict(breakdown)}
    if window is not None:
        usage["modelContextWindow"] = window
    return {"threadId": THREAD, "turnId": TURN, "tokenUsage": usage}


def test_token_usage_becomes_a_context_usage_event() -> None:
    translator, _ = _fresh()
    out = translator.translate(
        schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
        _notify(
            schema.METHOD_THREAD_TOKEN_USAGE_UPDATED, _usage_wire(window=400_000)
        ),
    )
    assert [type(e) for e in out] == [ev.ContextUsage]
    usage = out[0]
    assert isinstance(usage, ev.ContextUsage)
    assert (usage.used_tokens, usage.max_tokens) == (4_000, 400_000)
    assert usage.percentage == pytest.approx(1.0)
    assert usage.model == "gpt-5-codex"


def test_token_usage_without_a_context_window_emits_nothing() -> None:
    translator, _ = _fresh()
    assert (
        translator.translate(
            schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
            _notify(schema.METHOD_THREAD_TOKEN_USAGE_UPDATED, _usage_wire(window=None)),
        )
        == []
    )
    assert translator.counters.skipped_context_usage == 1


def test_usage_named_for_another_turn_is_not_billed_to_the_live_one() -> None:
    translator, _ = _fresh()
    stale = _usage_wire(window=400_000)
    stale["turnId"] = "tn_previous"
    translator.translate(
        schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
        _notify(schema.METHOD_THREAD_TOKEN_USAGE_UPDATED, stale),
    )
    end = _turn_completed(translator)[-1]
    assert isinstance(end, ev.TurnEnd)
    assert end.usage is None


def test_the_turns_last_usage_rides_its_turn_end() -> None:
    translator, _ = _fresh()
    translator.translate(
        schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
        _notify(schema.METHOD_THREAD_TOKEN_USAGE_UPDATED, _usage_wire(window=400_000)),
    )
    end = _turn_completed(translator)[-1]
    assert isinstance(end, ev.TurnEnd)
    assert end.usage == {
        "input_tokens": 3_000,
        "output_tokens": 1_000,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 42,
        "reasoning_output_tokens": 200,
        "total_tokens": 4_000,
    }


# ---------------------------------------------------------------------------
# 8. Thread status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [("active", "working"), ("idle", "idle")],
)
def test_thread_status_maps_to_a_status_event(status: str, expected: str) -> None:
    translator, _ = _fresh()
    out = translator.translate(
        schema.METHOD_THREAD_STATUS_CHANGED,
        _notify(
            schema.METHOD_THREAD_STATUS_CHANGED,
            {"threadId": THREAD, "status": {"type": status}},
        ),
    )
    assert [type(e) for e in out] == [ev.Status]
    assert out[0].state == expected  # type: ignore[attr-defined]


def test_not_loaded_thread_status_emits_nothing() -> None:
    translator, _ = _fresh()
    assert (
        translator.translate(
            schema.METHOD_THREAD_STATUS_CHANGED,
            _notify(
                schema.METHOD_THREAD_STATUS_CHANGED,
                {"threadId": THREAD, "status": {"type": "notLoaded"}},
            ),
        )
        == []
    )


def test_system_error_thread_status_emits_an_error_event() -> None:
    translator, _ = _fresh()
    out = translator.translate(
        schema.METHOD_THREAD_STATUS_CHANGED,
        _notify(
            schema.METHOD_THREAD_STATUS_CHANGED,
            {"threadId": THREAD, "status": {"type": "systemError"}},
        ),
    )
    assert [type(e) for e in out] == [ev.ErrorEvent]
    assert out[0].code == tr.THREAD_SYSTEM_ERROR_CODE  # type: ignore[attr-defined]
    assert out[0].turn_id == TURN  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 9. Context compaction
# ---------------------------------------------------------------------------


def test_context_compaction_toggles_condensing() -> None:
    translator, _ = _fresh()
    start = _item_started(translator, {"type": "contextCompaction", "id": "it_z"})
    done = _item_completed(translator, {"type": "contextCompaction", "id": "it_z"})
    assert [type(e) for e in start + done] == [ev.Condensing, ev.Condensing]
    assert start[0].active is True  # type: ignore[attr-defined]
    assert done[0].active is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 10. Purity
# ---------------------------------------------------------------------------


def test_the_translator_module_never_logs() -> None:
    source = Path(tr.__file__).read_text(encoding="utf-8")
    assert "import logging" not in source
    assert "log_event" not in source
    assert "print(" not in source


def test_reset_clears_stream_state_but_keeps_the_thread() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", "partial")
    translator.reset()
    assert translator.turn_id is None
    # The same item id is a fresh item after a reset, so its final text renders.
    out = _item_completed(translator, {"type": "agentMessage", "id": "it_a", "text": "x"})
    assert [type(e) for e in out] == [ev.TextDelta]


def test_counters_are_a_frozen_snapshot() -> None:
    translator, _ = _fresh()
    before = translator.counters
    _item_started(translator, {"type": "quantumThing", "id": "it_q"})
    assert before.unknown_items == 0
    assert translator.counters.unknown_items == 1
    with pytest.raises(Exception):
        before.unknown_items = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Golden transcripts
# ---------------------------------------------------------------------------


def _replay(name: str) -> tuple[tr.CodexEventTranslator, list[object]]:
    translator = tr.CodexEventTranslator(thread_id=THREAD)
    out: list[object] = []
    for line in (TRANSCRIPT_ROOT / name).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        method = frame["method"]
        try:
            params = schema.parse_server_notification(method, frame["params"])
        except schema.CodexSchemaError:  # pragma: no cover - fixture guard
            pytest.fail(f"fixture {name} carries an unparseable {method} frame")
        out += translator.translate(method, params)
    return translator, out


def test_ordinary_turn_transcript_translates_to_the_expected_stream() -> None:
    translator, out = _replay("ordinary_turn.jsonl")
    assert [type(e).__name__ for e in out] == [
        "TurnStart",
        "ThinkingDelta",
        "TextDelta",
        "TextDelta",
        "ToolCall",
        "ToolResult",
        "ToolCall",
        "ToolResult",
        "ContextUsage",
        "TurnEnd",
    ]
    calls = [e for e in out if isinstance(e, ev.ToolCall)]
    results = [e for e in out if isinstance(e, ev.ToolResult)]
    assert [c.name for c in calls] == ["Bash", "Edit"]
    assert [c.tool_id for c in calls] == [r.tool_id for r in results]
    assert all(r.ok for r in results)
    end = out[-1]
    assert isinstance(end, ev.TurnEnd)
    assert (end.turn_id, end.outcome) == ("turn_ord_1", "success")
    assert translator.counters.unknown_items == 0
    assert translator.counters.unknown_notifications == 0


def test_interrupted_turn_transcript_survives_an_unknown_item() -> None:
    translator, out = _replay("interrupted_turn.jsonl")
    assert [type(e).__name__ for e in out] == [
        "TurnStart",
        "TextDelta",
        "ToolCall",
        "ToolResult",
        "ErrorEvent",
        "TurnEnd",
    ]
    end = out[-1]
    assert isinstance(end, ev.TurnEnd)
    assert (end.turn_id, end.outcome) == ("turn_int_1", "interrupted")
    result = out[3]
    assert isinstance(result, ev.ToolResult)
    assert result.ok is False
    assert translator.counters.unknown_items == 2
