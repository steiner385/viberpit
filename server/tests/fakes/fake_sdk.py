"""Fake SDK client + builders emitting REAL claude_agent_sdk message objects.
Lets the translator/driver be tested without spawning `claude` or spending quota."""
from __future__ import annotations
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock)

# ---- builders (construct real SDK objects) -------------------------------------
def sys_init(session_id: str = "sess-1", model: str | None = None) -> SystemMessage:
    data: dict = {"session_id": session_id}
    if model is not None:
        data["model"] = model
    return SystemMessage(subtype="init", data=data)

def sys_compact_boundary(trigger: str = "auto", pre_tokens: int | None = None) -> SystemMessage:
    return SystemMessage(subtype="compact_boundary",
                         data={"compact_metadata": {"trigger": trigger, "pre_tokens": pre_tokens}})

def stream_start(uuid: str = "u1", sid: str = "sess-1", pid: str | None = None) -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid, event={"type": "message_start"},
                       parent_tool_use_id=pid)

def stream_text(text: str, uuid: str = "u1", sid: str = "sess-1", pid: str | None = None) -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid,
                       event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": text}},
                       parent_tool_use_id=pid)

def stream_thinking(text: str, uuid: str = "u1", sid: str = "sess-1", pid: str | None = None) -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid,
                       event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta", "thinking": text}},
                       parent_tool_use_id=pid)

def assistant(*blocks, uuid: str = "u1", parent_tool_use_id=None, model: str = "claude") -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=model, uuid=uuid,
                            parent_tool_use_id=parent_tool_use_id)

def tool_use(tool_id: str, name: str, inp: dict, uuid: str = "u1", pid: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=inp)],
                            model="claude", uuid=uuid, parent_tool_use_id=pid)

def user_tool_result(tool_id: str, content, is_error: bool = False, uuid: str = "u2",
                     pid: str | None = None) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)],
                       uuid=uuid, parent_tool_use_id=pid)

def result(session_id: str = "sess-1", usage: dict | None = None, uuid: str = "u9",
           cost: float | None = None, subtype: str = "success", is_error: bool = False,
           result: str | None = None, errors: list[str] | None = None,
           api_error_status: int | None = None) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id=session_id, usage=usage, uuid=uuid,
                         total_cost_usd=cost, result=result, errors=errors,
                         api_error_status=api_error_status)

# ---- fake client --------------------------------------------------------------
class FakeSDKClient:
    """Implements server.sessions.sdk_contract.SDKClientProtocol."""
    def __init__(self, messages, on_can_use_tool=None, server_info=None, context_usage=None):
        self._messages = messages
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.model = None
        self.sent: list[str] = []
        self._server_info = server_info
        self._context_usage = context_usage or {}

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt: str): self.sent.append(prompt)
    async def receive_response(self):
        for m in self._messages:
            if self.interrupted: break
            yield m
    async def interrupt(self): self.interrupted = True
    async def set_permission_mode(self, mode: str): self.permission_mode = mode
    async def set_model(self, model=None): self.model = model
    async def get_server_info(self): return self._server_info
    async def get_context_usage(self): return self._context_usage
