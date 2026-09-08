"""Codex thread/turn/item vocabulary: outbound params, inbound parsers, approvals.

Phase 4 widens the pinned contract from "read the account" to "run a session".
The properties these tests lock down are the ones a session's survival depends on:

* every outbound param round-trips through ``to_wire()`` into exactly the wire
  shape the pinned schema declares, and back through
  ``validate_client_request_params``;
* every supported notification parses, and a malformed one raises
  :class:`CodexSchemaError` so ``protocol.py`` counts and drops it;
* an **unknown thread item type never raises** — it degrades to
  :class:`UnknownThreadItem` — because a Codex release that adds a nineteenth
  item variant must not end a live session;
* approval decisions are restricted to the four string variants ccswitch can
  actually offer a human, and the object-shaped policy-amendment variants are
  refused outbound;
* the thread surface is **not** credential-bearing: a client built with
  ``external_auth_enabled=False`` may still start threads and turns.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.codex import protocol, schema
from server.codex.schema import CodexSchemaError
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer


# ---------------------------------------------------------------------------
# Method vocabulary
# ---------------------------------------------------------------------------

THREAD_METHODS = (
    schema.METHOD_THREAD_START,
    schema.METHOD_THREAD_RESUME,
    schema.METHOD_THREAD_READ,
    schema.METHOD_THREAD_ARCHIVE,
    schema.METHOD_TURN_START,
    schema.METHOD_TURN_STEER,
    schema.METHOD_TURN_INTERRUPT,
    schema.METHOD_TURN_SETTINGS_UPDATE,
)

THREAD_NOTIFICATIONS = (
    "thread/started",
    "thread/status/changed",
    "thread/tokenUsage/updated",
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/commandExecution/outputDelta",
)


def test_every_thread_method_is_callable() -> None:
    for method in THREAD_METHODS:
        assert method in schema.SUPPORTED_CLIENT_REQUESTS, method


def test_thread_methods_are_not_credential_bearing() -> None:
    """The whole point: `experimentalApi=false` must still run sessions."""
    for method in THREAD_METHODS:
        assert method not in schema.EXTERNAL_AUTH_ONLY_CLIENT_REQUESTS, method
    assert schema.EXTERNAL_AUTH_ONLY_CLIENT_REQUESTS == frozenset(
        {schema.METHOD_LOGIN_START}
    )
    assert schema.EXTERNAL_AUTH_ONLY_SERVER_REQUESTS == frozenset(
        {schema.METHOD_AUTH_TOKENS_REFRESH}
    )


def test_thread_notifications_are_supported() -> None:
    for method in THREAD_NOTIFICATIONS:
        assert method in schema.SUPPORTED_SERVER_NOTIFICATIONS, method
    # Raw chain of thought is deliberately NOT forwarded.
    assert "item/reasoning/textDelta" not in schema.SUPPORTED_SERVER_NOTIFICATIONS


def test_only_the_two_answerable_approvals_are_supported() -> None:
    assert schema.METHOD_COMMAND_EXECUTION_APPROVAL in schema.SUPPORTED_SERVER_REQUESTS
    assert schema.METHOD_FILE_CHANGE_APPROVAL in schema.SUPPORTED_SERVER_REQUESTS
    # ccswitch has no vocabulary for a permissions grant, so it stays unsupported.
    assert "item/permissions/requestApproval" not in schema.SUPPORTED_SERVER_REQUESTS


def test_supported_sets_stay_inside_the_pinned_inventory() -> None:
    assert schema.SUPPORTED_SERVER_NOTIFICATIONS <= schema.DECLARED_SERVER_NOTIFICATIONS
    assert schema.SUPPORTED_SERVER_REQUESTS <= schema.DECLARED_SERVER_REQUESTS


# ---------------------------------------------------------------------------
# Outbound params — to_wire() round trips
# ---------------------------------------------------------------------------


def _roundtrip(method: str, params: Any) -> dict[str, Any]:
    wire = params.to_wire()
    assert schema.validate_client_request_params(method, wire) == wire
    return wire


def test_thread_start_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_THREAD_START,
        schema.ThreadStartParams(
            cwd="/repo",
            model="gpt-5-codex",
            sandbox="workspace-write",
            approval_policy="on-request",
            developer_instructions="be terse",
            personality="pragmatic",
        ),
    )
    assert wire == {
        "cwd": "/repo",
        "model": "gpt-5-codex",
        "sandbox": "workspace-write",
        "approvalPolicy": "on-request",
        "developerInstructions": "be terse",
        "personality": "pragmatic",
    }


def test_thread_start_params_omit_unset_fields() -> None:
    assert schema.ThreadStartParams().to_wire() == {}


def test_thread_start_rejects_an_unknown_sandbox_mode() -> None:
    with pytest.raises(CodexSchemaError):
        schema.ThreadStartParams(sandbox="yolo")


def test_thread_start_rejects_an_unknown_approval_policy() -> None:
    with pytest.raises(CodexSchemaError):
        schema.ThreadStartParams(approval_policy="always")


def test_thread_start_refuses_sandbox_and_permissions_together() -> None:
    """The pinned schema says the named profile cannot be combined with `sandbox`."""
    with pytest.raises(CodexSchemaError):
        schema.ThreadStartParams(sandbox="read-only", permissions="my-profile")


def test_thread_resume_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_THREAD_RESUME,
        schema.ThreadResumeParams(
            thread_id="t1", cwd="/repo", model="gpt-5-codex", sandbox="read-only"
        ),
    )
    assert wire == {
        "threadId": "t1",
        "cwd": "/repo",
        "model": "gpt-5-codex",
        "sandbox": "read-only",
    }


def test_thread_read_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_THREAD_READ,
        schema.ThreadReadParams(thread_id="t1", include_turns=True),
    )
    assert wire == {"threadId": "t1", "includeTurns": True}


def test_thread_archive_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_THREAD_ARCHIVE, schema.ThreadArchiveParams(thread_id="t1")
    )
    assert wire == {"threadId": "t1"}


def test_turn_start_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_TURN_START,
        schema.TurnStartParams(
            thread_id="t1",
            input=(
                schema.TextUserInput(text="hello"),
                schema.LocalImageUserInput(path="/tmp/a.png"),
            ),
            model="gpt-5-codex",
            effort="high",
            cwd="/repo",
            approval_policy="never",
        ),
    )
    assert wire == {
        "threadId": "t1",
        "input": [
            {"type": "text", "text": "hello"},
            {"type": "localImage", "path": "/tmp/a.png"},
        ],
        "model": "gpt-5-codex",
        "effort": "high",
        "cwd": "/repo",
        "approvalPolicy": "never",
    }


def test_turn_start_requires_input() -> None:
    with pytest.raises(CodexSchemaError):
        schema.TurnStartParams(thread_id="t1", input=())


def test_turn_steer_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_TURN_STEER,
        schema.TurnSteerParams(
            thread_id="t1",
            expected_turn_id="u1",
            input=(schema.TextUserInput(text="stop"),),
        ),
    )
    assert wire == {
        "threadId": "t1",
        "expectedTurnId": "u1",
        "input": [{"type": "text", "text": "stop"}],
    }


def test_turn_interrupt_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_TURN_INTERRUPT,
        schema.TurnInterruptParams(thread_id="t1", turn_id="u1"),
    )
    assert wire == {"threadId": "t1", "turnId": "u1"}


def test_turn_settings_update_params_round_trip() -> None:
    wire = _roundtrip(
        schema.METHOD_TURN_SETTINGS_UPDATE,
        schema.TurnSettingsUpdateParams(
            thread_id="t1", turn_id="u1", model="gpt-5-codex", effort="low"
        ),
    )
    assert wire == {
        "threadId": "t1",
        "turnId": "u1",
        "model": "gpt-5-codex",
        "effort": "low",
    }


def test_client_request_params_reject_unknown_fields() -> None:
    with pytest.raises(CodexSchemaError):
        schema.validate_client_request_params(
            schema.METHOD_THREAD_READ, {"threadId": "t1", "wat": 1}
        )


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

THREAD_WIRE: dict[str, Any] = {
    "id": "t1",
    "cliVersion": "0.153.2",
    "createdAt": 1_700_000_000,
    "updatedAt": 1_700_000_100,
    "cwd": "/repo",
    "ephemeral": False,
    "modelProvider": "openai",
    "model": "gpt-5-codex",
    "reasoningEffort": "medium",
    "preview": "hello",
    "projectId": None,
    "sessionId": "s1",
    "source": "appServer",
    "status": {"type": "idle"},
    "turns": [],
    "name": "a thread",
}

START_RESPONSE_WIRE: dict[str, Any] = {
    "thread": THREAD_WIRE,
    "model": "gpt-5-codex",
    "modelProvider": "openai",
    "reasoningEffort": "medium",
    "cwd": "/repo",
    "approvalPolicy": "on-request",
    "approvalsReviewer": "user",
    "sandbox": {"type": "workspaceWrite", "networkAccess": False},
}


def test_thread_start_response_parses() -> None:
    parsed = schema.parse_client_response(
        schema.METHOD_THREAD_START, START_RESPONSE_WIRE
    )
    assert isinstance(parsed, schema.ThreadStartResponse)
    assert parsed.thread.id == "t1"
    assert parsed.thread.status.type == "idle"
    assert parsed.model == "gpt-5-codex"
    assert parsed.reasoning_effort == "medium"
    assert parsed.sandbox == "workspaceWrite"
    assert parsed.approval_policy == "on-request"


def test_thread_start_response_accepts_a_granular_approval_policy() -> None:
    """`AskForApproval` is a union; only the discriminator is retained."""
    wire = dict(START_RESPONSE_WIRE)
    wire["approvalPolicy"] = {
        "granular": {"mcp_elicitations": True, "rules": True, "sandbox_approval": True}
    }
    parsed = schema.parse_thread_start_response(wire)
    assert parsed.approval_policy == "granular"


def test_thread_resume_response_carries_turns() -> None:
    thread = dict(THREAD_WIRE)
    thread["turns"] = [
        {
            "id": "u1",
            "status": "completed",
            "items": [{"type": "agentMessage", "id": "i1", "text": "hi"}],
            "startedAt": 1_700_000_010,
            "completedAt": 1_700_000_020,
        }
    ]
    wire = dict(START_RESPONSE_WIRE) | {"thread": thread}
    parsed = schema.parse_client_response(schema.METHOD_THREAD_RESUME, wire)
    assert isinstance(parsed, schema.ThreadResumeResponse)
    turn = parsed.thread.turns[0]
    assert turn.status is schema.TurnStatus.COMPLETED
    assert isinstance(turn.items[0], schema.AgentMessageItem)


def test_thread_read_response_parses() -> None:
    parsed = schema.parse_client_response(
        schema.METHOD_THREAD_READ, {"thread": THREAD_WIRE}
    )
    assert isinstance(parsed, schema.ThreadReadResponse)
    assert parsed.thread.preview == "hello"


def test_thread_archive_and_turn_interrupt_responses_are_empty() -> None:
    assert isinstance(
        schema.parse_client_response(schema.METHOD_THREAD_ARCHIVE, {}),
        schema.ThreadArchiveResponse,
    )
    assert isinstance(
        schema.parse_client_response(schema.METHOD_TURN_INTERRUPT, {}),
        schema.TurnInterruptResponse,
    )


def test_turn_start_response_parses() -> None:
    parsed = schema.parse_client_response(
        schema.METHOD_TURN_START,
        {"turn": {"id": "u1", "status": "inProgress", "items": []}},
    )
    assert isinstance(parsed, schema.TurnStartResponse)
    assert parsed.turn.status is schema.TurnStatus.IN_PROGRESS


def test_turn_steer_response_parses() -> None:
    parsed = schema.parse_client_response(schema.METHOD_TURN_STEER, {"turnId": "u2"})
    assert isinstance(parsed, schema.TurnSteerResponse)
    assert parsed.turn_id == "u2"


def test_turn_settings_update_response_parses() -> None:
    parsed = schema.parse_client_response(
        schema.METHOD_TURN_SETTINGS_UPDATE, {"status": "applied"}
    )
    assert isinstance(parsed, schema.TurnSettingsUpdateResponse)
    assert parsed.status == "applied"


def test_turn_settings_update_response_rejects_an_unknown_status() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_client_response(
            schema.METHOD_TURN_SETTINGS_UPDATE, {"status": "maybe"}
        )


def test_turn_error_is_parsed_from_a_failed_turn() -> None:
    parsed = schema.parse_turn(
        {
            "id": "u1",
            "status": "failed",
            "items": [],
            "error": {"message": "boom", "codexErrorInfo": "serverOverloaded"},
        },
        "turn",
    )
    assert parsed.status is schema.TurnStatus.FAILED
    assert parsed.error is not None
    assert parsed.error.message == "boom"
    assert parsed.error.codex_error_info == "serverOverloaded"


# ---------------------------------------------------------------------------
# Thread items
# ---------------------------------------------------------------------------


def test_unknown_thread_item_never_raises() -> None:
    """A future Codex release adding an item type must not end a session."""
    parsed = schema.parse_thread_item({"type": "quantumThing", "id": "i9"})
    assert isinstance(parsed, schema.UnknownThreadItem)
    assert parsed.type == "quantumThing"
    assert parsed.id == "i9"


def test_unknown_thread_item_survives_a_missing_id() -> None:
    parsed = schema.parse_thread_item({"type": "quantumThing"})
    assert isinstance(parsed, schema.UnknownThreadItem)
    assert parsed.id == ""


def test_thread_item_that_is_not_an_object_raises() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_thread_item(["nope"])


def test_agent_message_item() -> None:
    item = schema.parse_thread_item({"type": "agentMessage", "id": "i1", "text": "hi"})
    assert item == schema.AgentMessageItem(id="i1", text="hi")


def test_user_message_item_keeps_its_text() -> None:
    item = schema.parse_thread_item(
        {
            "type": "userMessage",
            "id": "i1",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "localImage", "path": "/tmp/a.png"},
                {"type": "audio", "url": "https://example.invalid/a.wav"},
            ],
        }
    )
    assert isinstance(item, schema.UserMessageItem)
    assert item.text == "hello"
    assert isinstance(item.content[2], schema.UnknownUserInput)


def test_reasoning_item_keeps_only_the_summary() -> None:
    item = schema.parse_thread_item(
        {
            "type": "reasoning",
            "id": "i1",
            "summary": ["planning"],
            "content": ["raw chain of thought"],
        }
    )
    assert isinstance(item, schema.ReasoningItem)
    assert item.summary == ("planning",)
    assert not hasattr(item, "content")


def test_command_execution_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "commandExecution",
            "id": "i1",
            "command": "ls -la",
            "commandActions": [],
            "cwd": "/repo",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "total 0\n",
            "durationMs": 12,
        }
    )
    assert item == schema.CommandExecutionItem(
        id="i1",
        command="ls -la",
        cwd="/repo",
        status="completed",
        exit_code=0,
        aggregated_output="total 0\n",
        duration_ms=12,
    )


def test_file_change_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "fileChange",
            "id": "i1",
            "status": "completed",
            "changes": [
                {"path": "/repo/a.py", "kind": {"type": "update"}, "diff": "@@"},
                {"path": "/repo/b.py", "kind": {"type": "add"}, "diff": "@@"},
            ],
        }
    )
    assert isinstance(item, schema.FileChangeItem)
    assert item.changes == (
        schema.FileChange(path="/repo/a.py", kind="update"),
        schema.FileChange(path="/repo/b.py", kind="add"),
    )


def test_mcp_tool_call_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "mcpToolCall",
            "id": "i1",
            "server": "linear",
            "tool": "search",
            "arguments": {"q": "bug"},
            "status": "completed",
        }
    )
    assert isinstance(item, schema.McpToolCallItem)
    assert item.server == "linear"
    assert item.tool == "search"
    assert item.arguments == {"q": "bug"}


def test_web_search_item() -> None:
    item = schema.parse_thread_item(
        {"type": "webSearch", "id": "i1", "query": "codex app-server"}
    )
    assert item == schema.WebSearchItem(id="i1", query="codex app-server")


def test_plan_item() -> None:
    item = schema.parse_thread_item({"type": "plan", "id": "i1", "text": "1. do it"})
    assert item == schema.PlanItem(id="i1", text="1. do it")


def test_sub_agent_activity_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "subAgentActivity",
            "id": "i1",
            "agentPath": "reviewer",
            "agentThreadId": "t2",
            "kind": "started",
        }
    )
    assert item == schema.SubAgentActivityItem(
        id="i1", agent_path="reviewer", agent_thread_id="t2", kind="started"
    )


def test_collab_agent_tool_call_item_drops_the_prompt() -> None:
    item = schema.parse_thread_item(
        {
            "type": "collabAgentToolCall",
            "id": "i1",
            "tool": "spawnAgent",
            "model": "gpt-5-codex",
            "prompt": "secret prompt text",
            "status": "inProgress",
            "agentsStates": {},
            "receiverThreadIds": [],
            "senderThreadId": "t1",
        }
    )
    assert isinstance(item, schema.CollabAgentToolCallItem)
    assert item.tool == "spawnAgent"
    assert item.model == "gpt-5-codex"
    assert "secret prompt text" not in repr(item)


def test_collab_agent_tool_call_item_captures_parent_child_identity() -> None:
    """#1274: `senderThreadId`/`receiverThreadIds`/`agentsStates` used to be

    silently dropped, even though the pinned schema requires all three — they
    are the real parent/child correlation for Codex's collaboration mode.
    """
    item = schema.parse_thread_item(
        {
            "type": "collabAgentToolCall",
            "id": "i1",
            "tool": "spawnAgent",
            "status": "completed",
            "senderThreadId": "th_parent",
            "receiverThreadIds": ["th_child"],
            "agentsStates": {
                "th_child": {"status": "running", "message": "reviewing diff"}
            },
        }
    )
    assert isinstance(item, schema.CollabAgentToolCallItem)
    assert item.sender_thread_id == "th_parent"
    assert item.receiver_thread_ids == ("th_child",)
    assert item.agents_states == {
        "th_child": schema.CollabAgentState(status="running", message="reviewing diff")
    }


def test_collab_agent_tool_call_item_tolerates_a_missing_agents_states() -> None:
    """`agentsStates` is required by the pin, but a future variant that omits it

    must degrade to "no known state" rather than end the session (the same
    forward-compat rule every other collection field in this parser follows).
    """
    item = schema.parse_thread_item(
        {
            "type": "collabAgentToolCall",
            "id": "i1",
            "tool": "wait",
            "status": "completed",
            "senderThreadId": "th_parent",
            "receiverThreadIds": [],
        }
    )
    assert isinstance(item, schema.CollabAgentToolCallItem)
    assert item.agents_states == {}
    assert item.receiver_thread_ids == ()


def test_dynamic_tool_call_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "dynamicToolCall",
            "id": "i1",
            "tool": "custom",
            "arguments": {"a": 1},
            "status": "completed",
        }
    )
    assert isinstance(item, schema.DynamicToolCallItem)
    assert item.tool == "custom"


def test_function_call_output_item() -> None:
    item = schema.parse_thread_item(
        {
            "type": "functionCallOutput",
            "id": "i1",
            "name": "shell",
            "output": "done",
        }
    )
    assert item == schema.FunctionCallOutputItem(id="i1", name="shell")


def test_image_view_item() -> None:
    item = schema.parse_thread_item(
        {"type": "imageView", "id": "i1", "path": "/repo/a.png"}
    )
    assert item == schema.ImageViewItem(id="i1", path="/repo/a.png")


def test_context_compaction_item() -> None:
    item = schema.parse_thread_item({"type": "contextCompaction", "id": "i1"})
    assert item == schema.ContextCompactionItem(id="i1")


def test_a_known_item_with_a_broken_shape_raises() -> None:
    """Known-but-malformed is a contract violation, not a new item type."""
    with pytest.raises(CodexSchemaError):
        schema.parse_thread_item({"type": "agentMessage", "id": "i1"})


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_thread_started_notification() -> None:
    parsed = schema.parse_server_notification("thread/started", {"thread": THREAD_WIRE})
    assert isinstance(parsed, schema.ThreadStartedParams)
    assert parsed.thread.id == "t1"


def test_thread_status_changed_notification() -> None:
    parsed = schema.parse_server_notification(
        "thread/status/changed",
        {"threadId": "t1", "status": {"type": "active", "activeFlags": []}},
    )
    assert isinstance(parsed, schema.ThreadStatusChangedParams)
    assert parsed.status.type == "active"


def test_thread_status_changed_keeps_active_flags() -> None:
    parsed = schema.parse_server_notification(
        "thread/status/changed",
        {
            "threadId": "t1",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
        },
    )
    assert isinstance(parsed, schema.ThreadStatusChangedParams)
    assert parsed.status.active_flags == ("waitingOnApproval",)


def test_thread_token_usage_updated_notification() -> None:
    parsed = schema.parse_server_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "t1",
            "turnId": "u1",
            "tokenUsage": {
                "modelContextWindow": 272_000,
                "total": {
                    "inputTokens": 10,
                    "cachedInputTokens": 2,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 1,
                    "totalTokens": 13,
                },
                "last": {
                    "inputTokens": 1,
                    "cachedInputTokens": 0,
                    "outputTokens": 1,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 2,
                },
            },
        },
    )
    assert isinstance(parsed, schema.ThreadTokenUsageUpdatedParams)
    assert parsed.token_usage.model_context_window == 272_000
    assert parsed.token_usage.total.total_tokens == 13


def test_thread_token_usage_tolerates_a_null_context_window() -> None:
    parsed = schema.parse_server_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "t1",
            "turnId": "u1",
            "tokenUsage": {
                "modelContextWindow": None,
                "total": {
                    "inputTokens": 0,
                    "cachedInputTokens": 0,
                    "outputTokens": 0,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 0,
                },
                "last": {
                    "inputTokens": 0,
                    "cachedInputTokens": 0,
                    "outputTokens": 0,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 0,
                },
            },
        },
    )
    assert isinstance(parsed, schema.ThreadTokenUsageUpdatedParams)
    assert parsed.token_usage.model_context_window is None


def test_turn_started_and_completed_notifications() -> None:
    started = schema.parse_server_notification(
        "turn/started",
        {"threadId": "t1", "turn": {"id": "u1", "status": "inProgress", "items": []}},
    )
    assert isinstance(started, schema.TurnStartedParams)
    completed = schema.parse_server_notification(
        "turn/completed",
        {"threadId": "t1", "turn": {"id": "u1", "status": "completed", "items": []}},
    )
    assert isinstance(completed, schema.TurnCompletedParams)
    assert completed.turn.status is schema.TurnStatus.COMPLETED


def test_item_started_and_completed_notifications() -> None:
    started = schema.parse_server_notification(
        "item/started",
        {
            "threadId": "t1",
            "turnId": "u1",
            "startedAtMs": 1_700_000_000_000,
            "item": {"type": "contextCompaction", "id": "i1"},
        },
    )
    assert isinstance(started, schema.ItemStartedParams)
    assert started.started_at_ms == 1_700_000_000_000
    completed = schema.parse_server_notification(
        "item/completed",
        {
            "threadId": "t1",
            "turnId": "u1",
            "completedAtMs": 1_700_000_001_000,
            "item": {"type": "agentMessage", "id": "i1", "text": "hi"},
        },
    )
    assert isinstance(completed, schema.ItemCompletedParams)
    assert isinstance(completed.item, schema.AgentMessageItem)


def test_item_completed_with_an_unknown_item_still_parses() -> None:
    parsed = schema.parse_server_notification(
        "item/completed",
        {
            "threadId": "t1",
            "turnId": "u1",
            "completedAtMs": 1,
            "item": {"type": "sleep", "id": "i1", "durationMs": 5},
        },
    )
    assert isinstance(parsed, schema.ItemCompletedParams)
    assert isinstance(parsed.item, schema.UnknownThreadItem)


def test_delta_notifications() -> None:
    agent = schema.parse_server_notification(
        "item/agentMessage/delta",
        {"threadId": "t1", "turnId": "u1", "itemId": "i1", "delta": "he"},
    )
    assert isinstance(agent, schema.AgentMessageDeltaParams)
    assert agent.delta == "he"
    reasoning = schema.parse_server_notification(
        "item/reasoning/summaryTextDelta",
        {
            "threadId": "t1",
            "turnId": "u1",
            "itemId": "i1",
            "delta": "th",
            "summaryIndex": 0,
        },
    )
    assert isinstance(reasoning, schema.ReasoningSummaryTextDeltaParams)
    assert reasoning.summary_index == 0
    output = schema.parse_server_notification(
        "item/commandExecution/outputDelta",
        {"threadId": "t1", "turnId": "u1", "itemId": "i1", "delta": "ou"},
    )
    assert isinstance(output, schema.CommandExecutionOutputDeltaParams)
    assert output.delta == "ou"


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("thread/started", {}),
        ("thread/started", {"thread": {"id": "t1"}}),
        ("thread/status/changed", {"threadId": "t1", "status": {"type": "wat"}}),
        ("thread/status/changed", {"status": {"type": "idle"}}),
        ("thread/tokenUsage/updated", {"threadId": "t1", "turnId": "u1"}),
        ("turn/started", {"threadId": "t1"}),
        ("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "wat"}}),
        ("item/started", {"threadId": "t1", "turnId": "u1"}),
        ("item/completed", {"threadId": "t1", "turnId": "u1", "completedAtMs": "soon"}),
        ("item/agentMessage/delta", {"threadId": "t1", "turnId": "u1"}),
        (
            "item/reasoning/summaryTextDelta",
            {"threadId": "t1", "turnId": "u1", "itemId": "i1", "delta": "x"},
        ),
        ("item/commandExecution/outputDelta", {"threadId": "t1", "delta": "x"}),
    ],
)
def test_malformed_notifications_raise(method: str, payload: Any) -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_server_notification(method, payload)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def test_command_execution_approval_params_parse() -> None:
    parsed = schema.parse_command_execution_approval_params(
        {
            "itemId": "i1",
            "threadId": "t1",
            "turnId": "u1",
            "startedAtMs": 1_700_000_000_000,
            "command": "rm -rf /",
            "cwd": "/repo",
            "reason": "needs network",
            "availableDecisions": ["accept", "decline", "cancel"],
        }
    )
    assert parsed.item_id == "i1"
    assert parsed.command == "rm -rf /"
    assert parsed.available_decisions == ("accept", "decline", "cancel")


def test_command_execution_approval_drops_unofferable_decisions() -> None:
    """Object-shaped amendment variants are never presented to a human."""
    parsed = schema.parse_command_execution_approval_params(
        {
            "itemId": "i1",
            "threadId": "t1",
            "turnId": "u1",
            "startedAtMs": 1,
            "availableDecisions": [
                "accept",
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["x"]}},
                "decline",
            ],
        }
    )
    assert parsed.available_decisions == ("accept", "decline")


def test_command_execution_approval_params_require_ids() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_command_execution_approval_params(
            {"threadId": "t1", "turnId": "u1", "startedAtMs": 1}
        )


def test_file_change_approval_params_parse() -> None:
    parsed = schema.parse_file_change_approval_params(
        {
            "itemId": "i1",
            "threadId": "t1",
            "turnId": "u1",
            "startedAtMs": 1,
            "reason": "write outside the workspace",
            "grantRoot": "/repo/vendor",
        }
    )
    assert parsed.grant_root == "/repo/vendor"


@pytest.mark.parametrize("decision", sorted(schema.COMMAND_APPROVAL_DECISIONS))
def test_command_approval_response_round_trips(decision: str) -> None:
    response = schema.CommandExecutionApprovalResponse(decision=decision)
    assert response.to_wire() == {"decision": decision}
    assert schema.parse_command_execution_approval_response(response.to_wire()) == response


def test_command_approval_response_rejects_an_object_shaped_decision() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_command_execution_approval_response(
            {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": []}}}
        )


def test_command_approval_response_rejects_an_unknown_decision() -> None:
    with pytest.raises(CodexSchemaError):
        schema.CommandExecutionApprovalResponse(decision="allowAlways")


@pytest.mark.parametrize("decision", sorted(schema.FILE_CHANGE_APPROVAL_DECISIONS))
def test_file_change_approval_response_round_trips(decision: str) -> None:
    response = schema.FileChangeApprovalResponse(decision=decision)
    assert response.to_wire() == {"decision": decision}
    assert schema.parse_file_change_approval_response(response.to_wire()) == response


def test_file_change_approval_response_rejects_an_object_shaped_decision() -> None:
    with pytest.raises(CodexSchemaError):
        schema.parse_file_change_approval_response({"decision": {"accept": {}}})




# ---------------------------------------------------------------------------
# protocol.py — the thread surface works without the experimental API
# ---------------------------------------------------------------------------


async def _ready(fake: FakeCodexAppServer, **kwargs: Any) -> protocol.CodexAppServerClient:
    kwargs.setdefault("external_auth_enabled", False)
    kwargs.setdefault("request_timeout_s", 2.0)
    client = protocol.CodexAppServerClient(fake.transport, **kwargs)
    await client.start()
    await asyncio.gather(client.initialize(), fake.serve_initialize())
    return client


@pytest.mark.asyncio
async def test_thread_and_turn_start_work_without_external_auth() -> None:
    """The thread surface is not credential-bearing: `experimentalApi=false` runs."""
    async with FakeCodexAppServer() as fake:
        fake.rule(schema.METHOD_THREAD_START, result=START_RESPONSE_WIRE)
        fake.rule(
            schema.METHOD_TURN_START,
            result={"turn": {"id": "u1", "status": "inProgress", "items": []}},
        )
        client = await _ready(fake)
        fake.start_autopilot()
        try:
            assert client.external_auth_enabled is False
            started = await client.start_thread(schema.ThreadStartParams(cwd="/repo"))
            assert started.thread.id == "t1"
            turn = await client.start_turn(
                schema.TurnStartParams(
                    thread_id="t1", input=(schema.TextUserInput(text="hi"),)
                )
            )
            assert turn.turn.id == "u1"
        finally:
            await client.aclose()
        assert fake.frames_for(schema.METHOD_THREAD_START)[0]["params"] == {"cwd": "/repo"}


@pytest.mark.asyncio
async def test_login_start_is_still_refused_without_external_auth() -> None:
    """The gate that matters did not widen."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            with pytest.raises(protocol.CodexExternalAuthDisabled):
                await client.call(
                    schema.METHOD_LOGIN_START,
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": "x",
                        "chatgptAccountId": "y",
                    },
                )
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_the_other_thread_wrappers_reach_the_wire() -> None:
    async with FakeCodexAppServer() as fake:
        fake.rule(schema.METHOD_THREAD_RESUME, result=START_RESPONSE_WIRE)
        fake.rule(schema.METHOD_THREAD_READ, result={"thread": THREAD_WIRE})
        fake.rule(schema.METHOD_THREAD_ARCHIVE, result={})
        fake.rule(schema.METHOD_TURN_STEER, result={"turnId": "u2"})
        fake.rule(schema.METHOD_TURN_INTERRUPT, result={})
        fake.rule(schema.METHOD_TURN_SETTINGS_UPDATE, result={"status": "applied"})
        client = await _ready(fake)
        fake.start_autopilot()
        try:
            resumed = await client.resume_thread(
                schema.ThreadResumeParams(thread_id="t1")
            )
            assert resumed.thread.id == "t1"
            read = await client.read_thread(schema.ThreadReadParams(thread_id="t1"))
            assert read.thread.id == "t1"
            assert isinstance(
                await client.archive_thread(schema.ThreadArchiveParams(thread_id="t1")),
                schema.ThreadArchiveResponse,
            )
            steered = await client.steer_turn(
                schema.TurnSteerParams(
                    thread_id="t1",
                    expected_turn_id="u1",
                    input=(schema.TextUserInput(text="stop"),),
                )
            )
            assert steered.turn_id == "u2"
            assert isinstance(
                await client.interrupt_turn(
                    schema.TurnInterruptParams(thread_id="t1", turn_id="u1")
                ),
                schema.TurnInterruptResponse,
            )
            updated = await client.update_turn_settings(
                schema.TurnSettingsUpdateParams(
                    thread_id="t1", turn_id="u1", effort="low"
                )
            )
            assert updated.status == "applied"
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_approval_request_without_a_handler_is_method_not_found() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.request(
                schema.METHOD_COMMAND_EXECUTION_APPROVAL,
                {"itemId": "i1", "threadId": "t1", "turnId": "u1", "startedAtMs": 1},
                request_id=77,
            )
            reply = await fake.receive_reply(77)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND
            assert client.counters.unhandled_server_requests == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_approval_handler_answers_a_command_approval() -> None:
    seen: list[tuple[str, schema.ApprovalParams]] = []

    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        seen.append((method, params))
        return schema.CommandExecutionApprovalResponse(decision="decline")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                schema.METHOD_COMMAND_EXECUTION_APPROVAL,
                {
                    "itemId": "i1",
                    "threadId": "t1",
                    "turnId": "u1",
                    "startedAtMs": 1,
                    "command": "ls",
                },
                request_id=78,
            )
            reply = await fake.receive_reply(78)
            assert reply["result"] == {"decision": "decline"}
            assert seen[0][0] == schema.METHOD_COMMAND_EXECUTION_APPROVAL
            assert isinstance(seen[0][1], schema.CommandExecutionApprovalParams)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_approval_handler_answers_a_file_change_approval() -> None:
    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        assert isinstance(params, schema.FileChangeApprovalParams)
        return schema.FileChangeApprovalResponse(decision="acceptForSession")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                schema.METHOD_FILE_CHANGE_APPROVAL,
                {"itemId": "i1", "threadId": "t1", "turnId": "u1", "startedAtMs": 1},
                request_id=82,
            )
            reply = await fake.receive_reply(82)
            assert reply["result"] == {"decision": "acceptForSession"}
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_an_invalid_approval_request_never_reaches_the_handler() -> None:
    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:  # pragma: no cover - must never run
        raise AssertionError("handler must not see an invalid request")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                schema.METHOD_FILE_CHANGE_APPROVAL, {"threadId": "t1"}, request_id=79
            )
            reply = await fake.receive_reply(79)
            assert reply["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_a_raising_approval_handler_fails_closed() -> None:
    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        raise RuntimeError("approval registry unavailable")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                schema.METHOD_FILE_CHANGE_APPROVAL,
                {"itemId": "i1", "threadId": "t1", "turnId": "u1", "startedAtMs": 1},
                request_id=80,
            )
            reply = await fake.receive_reply(80)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_a_handler_returning_the_wrong_response_type_fails_closed() -> None:
    """A file-change request may not be answered with a command decision."""

    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        return schema.CommandExecutionApprovalResponse(decision="accept")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                schema.METHOD_FILE_CHANGE_APPROVAL,
                {"itemId": "i1", "threadId": "t1", "turnId": "u1", "startedAtMs": 1},
                request_id=83,
            )
            reply = await fake.receive_reply(83)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_permissions_approval_stays_unsupported_even_with_a_handler() -> None:
    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:  # pragma: no cover - must never run
        raise AssertionError("permissions approvals have no ccswitch vocabulary")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, approval_handler=handler)
        try:
            fake.request(
                "item/permissions/requestApproval", {"itemId": "i1"}, request_id=81
            )
            reply = await fake.receive_reply(81)
            assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND
            assert client.counters.unknown_server_requests == 1
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_approval_requests_are_shed_over_the_inflight_cap() -> None:
    """Shedding leaves an approval unanswered, which is never a granted one."""
    released = asyncio.Event()

    async def handler(
        method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        await released.wait()
        return schema.CommandExecutionApprovalResponse(decision="accept")

    async with FakeCodexAppServer() as fake:
        client = await _ready(
            fake, approval_handler=handler, max_inflight_server_requests=1
        )
        try:
            for request_id in (90, 91):
                fake.request(
                    schema.METHOD_COMMAND_EXECUTION_APPROVAL,
                    {
                        "itemId": "i1",
                        "threadId": "t1",
                        "turnId": "u1",
                        "startedAtMs": 1,
                    },
                    request_id=request_id,
                )
            await fake.drained()
            assert client.counters.shed_server_requests == 1
            assert 91 not in fake.replies
        finally:
            released.set()
            await client.aclose()
