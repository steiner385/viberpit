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

def stream_start(uuid: str = "u1", sid: str = "sess-1") -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid, event={"type": "message_start"})

def stream_text(text: str, uuid: str = "u1", sid: str = "sess-1") -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid,
                       event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": text}})

def stream_thinking(text: str, uuid: str = "u1", sid: str = "sess-1") -> StreamEvent:
    return StreamEvent(uuid=uuid, session_id=sid,
                       event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta", "thinking": text}})

def assistant(*blocks, uuid: str = "u1") -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude", uuid=uuid)

def tool_use(tool_id: str, name: str, inp: dict, uuid: str = "u1") -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=inp)],
                            model="claude", uuid=uuid)

def user_tool_result(tool_id: str, content, is_error: bool = False, uuid: str = "u2") -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)],
                       uuid=uuid)

def result(session_id: str = "sess-1", usage: dict | None = None, uuid: str = "u9",
           cost: float | None = None) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id=session_id, usage=usage, uuid=uuid,
                         total_cost_usd=cost)

# ---- fake client --------------------------------------------------------------
class FakeSDKClient:
    """Implements server.sessions.sdk_contract.SDKClientProtocol."""
    def __init__(self, messages, on_can_use_tool=None):
        self._messages = messages
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.sent: list[str] = []
        self.sent_structured: list[dict] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt):
        if isinstance(prompt, str):
            self.sent.append(prompt)
        else:
            async for msg in prompt:
                self.sent_structured.append(msg)
    async def receive_response(self):
        for m in self._messages:
            if self.interrupted: break
            yield m
    async def interrupt(self): self.interrupted = True
    async def set_permission_mode(self, mode: str): self.permission_mode = mode
