"""The exact slice of claude-agent-sdk that SessionDriver depends on.

Confirmed live in the Phase 0 spike (see docs/superpowers/spikes/2026-06-20-phase0-verdicts.md).
This Protocol is what the fake SDK client (tests) and the real client must both satisfy,
so the translator/driver are tested without spawning real `claude` or spending quota.
"""
from __future__ import annotations
from typing import Any, AsyncIterator, AsyncIterable, Protocol, runtime_checkable


@runtime_checkable
class SDKClientProtocol(Protocol):
    """Surface SessionDriver calls on the SDK client.

    The real implementation is `claude_agent_sdk.ClaudeSDKClient`, constructed with
    `ClaudeAgentOptions(cwd=..., env={...,'CLAUDE_CONFIG_DIR':cfg}, permission_mode=...,
    resume=..., can_use_tool=<async cb>, include_partial_messages=True)`.
    """

    async def __aenter__(self) -> "SDKClientProtocol": ...
    async def __aexit__(self, *exc: Any) -> bool | None: ...
    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]:
        """Yields, in order: SystemMessage, StreamEvent*, AssistantMessage, UserMessage,
        ResultMessage (and HookEventMessage/RateLimitEvent, which the translator ignores)."""
        ...
    async def interrupt(self) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...


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
