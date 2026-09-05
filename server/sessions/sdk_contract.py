"""The exact slice of claude-agent-sdk that ClaudeAgentDriver depends on.

Confirmed live in the Phase 0 spike (see docs/superpowers/spikes/2026-06-20-phase0-verdicts.md).
This Protocol is what the fake SDK client (tests) and the real client must both satisfy,
so the translator/driver are tested without spawning real `claude` or spending quota.
"""
from __future__ import annotations
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

# The SDK messages that belong to a model turn. Everything else on the stream is
# bookkeeping the CLI emits BETWEEN turns, with no ResultMessage behind it: hook
# lifecycle frames (``hook_started`` / ``hook_response`` / ``hook_progress`` for
# SessionStart hooks — ~1.5 s after connect and BEFORE any prompt, whenever the account
# config enables plugins; verified against claude 2.1.260 / claude-agent-sdk 0.2.105 on
# 2026-09-04) and background-task notices (``task_started`` … ``task_notification``).
# A consumer that infers "a turn is running" from ANY frame parks on a result that
# never comes (incident 2026-09-04: three fresh sessions stuck at "Working… · 1 queued",
# the initial prompt queued behind a phantom turn). A CLI-initiated turn announces
# itself with the user message the CLI injects — that, not the notice before it, is
# the turn's first frame.
TURN_MESSAGES: tuple[type, ...] = (UserMessage, AssistantMessage, StreamEvent, ResultMessage)


def is_turn_message(msg: Any) -> bool:
    """True when ``msg`` is part of a model turn (see ``TURN_MESSAGES``)."""
    return isinstance(msg, TURN_MESSAGES)


@runtime_checkable
class SDKClientProtocol(Protocol):
    """Surface ClaudeAgentDriver calls on the SDK client.

    The real implementation is `claude_agent_sdk.ClaudeSDKClient`, constructed with
    `ClaudeAgentOptions(cwd=..., env={...,'CLAUDE_CONFIG_DIR':cfg}, permission_mode=...,
    resume=..., can_use_tool=<async cb>, include_partial_messages=True)`.
    """

    async def __aenter__(self) -> "SDKClientProtocol": ...
    async def __aexit__(self, *exc: Any) -> bool | None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_messages(self) -> AsyncIterator[Any]:
        """ONE persistent stream for the life of the client — every message the CLI
        emits, in order: SystemMessage, StreamEvent*, AssistantMessage, UserMessage,
        ResultMessage (and HookEventMessage/RateLimitEvent, which the translator
        ignores). Ends only when the subprocess is gone. It is NOT quiet before the
        first prompt: SessionStart hook frames arrive right after connect (see
        ``TURN_MESSAGES``), so a consumer must not read "a message" as "a turn".

        The driver segments turns on ResultMessage itself and consumes this stream at
        ALL times (a prompt's drain, or the idle listener between prompts): the CLI
        also emits whole turns with no ``query()`` behind them — a background task's
        ``<task-notification>`` it injects as a user message — and the SDK's channel
        is bounded (100), so an unread stream both blocks the CLI on stdout and
        replays the stale turn into the next prompt. ``receive_response()`` (stop at
        the first result) is therefore NOT part of this contract."""
        ...
    async def interrupt(self) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    async def set_model(self, model: str | None = None) -> None: ...
    async def get_server_info(self) -> dict | None: ...
    async def get_context_usage(self) -> dict: ...


async def one_turn(client: SDKClientProtocol) -> AsyncIterator[Any]:
    """Yield ONE turn from ``client.receive_messages()`` — up to and including its
    ResultMessage. For the one-shot helper clients (title/summary/suggestion
    generation), which query once and read a single reply; the session driver has
    its own cursor and never uses this."""
    from claude_agent_sdk import ResultMessage
    async for msg in client.receive_messages():
        yield msg
        if isinstance(msg, ResultMessage):
            return


# Message-shape reference (consumed by translator.py). These mirror the SDK's dataclasses;
# the translator imports the real classes for isinstance checks and uses these field names.
#
#   SystemMessage   .subtype ("init"), .data: dict  (session metadata)
#   StreamEvent     .event: dict  — raw Anthropic stream event:
#                     content_block_start: {index, content_block:{type:'text'|'thinking'|'tool_use', id?, name?}}
#                     content_block_delta: {index, delta:{type:'text_delta',text} | {'thinking_delta',thinking}
#                                                        | {'input_json_delta',partial_json}}
#                     content_block_stop:  {index}
#   AssistantMessage .content: [TextBlock(text) | ThinkingBlock(thinking) | ToolUseBlock(id,name,input)]
#   UserMessage      .content: [ToolResultBlock(tool_use_id, content, is_error)]
#   ResultMessage    .session_id, .usage, .total_cost_usd
#
# Permission callback: async (tool_name:str, tool_input:dict, ctx) -> PermissionResultAllow|PermissionResultDeny
