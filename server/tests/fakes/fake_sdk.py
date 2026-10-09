"""Fake SDK client + builders emitting REAL claude_agent_sdk message objects.
Lets the translator/driver be tested without spawning `claude` or spending quota."""
from __future__ import annotations
import asyncio
from typing import Any
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, HookEventMessage)
from claude_agent_sdk.types import TaskNotificationStatus, TaskUpdatedStatus, TaskUsage

from server.sessions.sdk_contract import is_turn_message

# ---- builders (construct real SDK objects) -------------------------------------
def session_start_hook_frames() -> list:
    """What the real CLI (2.1.260, claude-agent-sdk 0.2.105) emits ~1.5 s after connect
    with NO prompt sent, for an account config that enables plugins with SessionStart
    hooks: hook_started / hook_response per hook, then hook_progress for an async one.
    Captured against the real binary on 2026-09-04. No ResultMessage follows them."""
    return [
        HookEventMessage(subtype="hook_started", data={
            "type": "system", "subtype": "hook_started", "hook_id": "h1",
            "hook_name": "SessionStart:startup", "hook_event": "SessionStart"}),
        HookEventMessage(subtype="hook_response", data={
            "type": "system", "subtype": "hook_response", "hook_id": "h1",
            "hook_name": "SessionStart:startup", "hook_event": "SessionStart",
            "output": '{"async": true}'}),
        SystemMessage(subtype="hook_progress", data={
            "type": "system", "subtype": "hook_progress", "hook_id": "h1",
            "hook_name": "SessionStart:startup", "hook_event": "SessionStart",
            "stdout": '{"async": true}'}),
    ]

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

def task_started(task_id: str, tool_use_id: str | None, description: str = "sleep probe",
                 subagent_type: str = "general-purpose", backgrounded: bool = False,
                 depth: int = 1, task_type: str = "local_agent", sid: str = "sess-1",
                 uuid: str = "ts1"):
    """system/task_started as claude 2.1.268 emits it when a Task sub-agent spawns."""
    from claude_agent_sdk import TaskStartedMessage
    data: dict[str, Any] = {"type": "system", "subtype": "task_started", "task_id": task_id,
            "description": description, "subagent_type": subagent_type,
            "is_backgrounded": backgrounded, "spawn_depth": depth, "task_type": task_type,
            "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    return TaskStartedMessage(subtype="task_started", data=data, task_id=task_id,
                              description=description, uuid=uuid, session_id=sid,
                              tool_use_id=tool_use_id, task_type=task_type)

def task_progress(task_id: str, tool_use_id: str | None, activity: str = "Running Bash",
                  total_tokens: int = 19591, tool_uses: int = 1, duration_ms: int = 4098,
                  last_tool: str | None = "Bash", sid: str = "sess-1", uuid: str = "tp1"):
    from claude_agent_sdk import TaskProgressMessage
    usage: TaskUsage = {"total_tokens": total_tokens, "tool_uses": tool_uses,
                        "duration_ms": duration_ms}
    data: dict[str, Any] = {"type": "system", "subtype": "task_progress", "task_id": task_id,
            "description": activity, "usage": usage, "last_tool_name": last_tool,
            "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    return TaskProgressMessage(subtype="task_progress", data=data, task_id=task_id,
                               description=activity, usage=usage, uuid=uuid, session_id=sid,
                               tool_use_id=tool_use_id, last_tool_name=last_tool)

def task_updated(task_id: str, status: TaskUpdatedStatus = "killed", sid: str = "sess-1",
                 uuid: str = "tu1"):
    from claude_agent_sdk import TaskUpdatedMessage
    patch: dict[str, Any] = {"status": status, "end_time": 1789150650196}
    data: dict[str, Any] = {"type": "system", "subtype": "task_updated", "task_id": task_id,
            "patch": patch, "uuid": uuid, "session_id": sid}
    return TaskUpdatedMessage(subtype="task_updated", data=data, task_id=task_id, patch=patch,
                              status=status, session_id=sid, uuid=uuid)

def task_notification(task_id: str, tool_use_id: str | None,
                      status: TaskNotificationStatus = "completed",
                      summary: str = "sleep probe", usage: TaskUsage | None = None,
                      sid: str = "sess-1", uuid: str = "tn1",
                      task_type: str | None = None):
    from claude_agent_sdk import TaskNotificationMessage
    data: dict[str, Any] = {"type": "system", "subtype": "task_notification", "task_id": task_id,
            "status": status, "output_file": f"/tmp/tasks/{task_id}.output",
            "summary": summary, "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    if usage is not None:
        data["usage"] = usage
    if task_type is not None:
        data["task_type"] = task_type
    return TaskNotificationMessage(subtype="task_notification", data=data, task_id=task_id,
                                   status=status, output_file=data["output_file"],
                                   summary=summary, uuid=uuid, session_id=sid,
                                   tool_use_id=tool_use_id, usage=usage)


async def drain_turn(stream) -> list:
    """Pull ONE turn — up to and including its ResultMessage — from a persistent
    ``receive_messages()`` stream: the segmentation the driver does. Pass the SAME
    generator object across calls; a stream that ends yields a short/empty list."""
    out = []
    async for m in stream:
        out.append(m)
        if isinstance(m, ResultMessage):
            break
    return out


# ---- fake client --------------------------------------------------------------
_EOF = object()   # inbox sentinel: the subprocess is gone, the stream ends


class FakeSDKClient:
    """Implements server.sessions.sdk_contract.SDKClientProtocol.

    Mirrors the real client's stream shape: ``receive_messages()`` is ONE persistent
    stream for the life of the client. Each ``query()`` delivers the scripted
    ``messages`` (one turn); :meth:`inject` delivers messages with NO query behind
    them — the CLI continuing on its own after a background-task notification, which
    is exactly what the driver's idle listener exists to drain. ``interrupt()`` does
    what the CLI does: drops the rest of the in-flight turn and flushes a synthetic
    ResultMessage so the turn still closes.

    Like the real CLI, the stream is NOT quiet before the first prompt: ``__aenter__``
    delivers ``startup_frames`` — by default the SessionStart hook frames the CLI emits
    right after connect (``session_start_hook_frames()``) — so every driver test runs
    against the pre-prompt stream shape that made #1009's listener open a phantom turn.
    Pass ``startup_frames=[]`` for a silent connect.
    """
    def __init__(self, messages, on_can_use_tool=None, server_info=None, context_usage=None,
                 startup_frames: list | None = None, mcp_status=None):
        self._messages = list(messages)
        self._startup_frames = (session_start_hook_frames() if startup_frames is None
                                else list(startup_frames))
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.stopped_tasks: list[str] = []
        self.permission_mode = "default"
        self.model = None
        self.sent: list[str] = []
        self._server_info = server_info
        self._context_usage = context_usage or {}
        self._mcp_status = mcp_status
        self.reconnected: list[str] = []
        self.toggled: list[tuple[str, bool]] = []
        self.toggle_error: Exception | None = None
        self._inbox: "asyncio.Queue | None" = None   # built lazily on the running loop
        self._turn_open = False

    def _q(self) -> "asyncio.Queue":
        if self._inbox is None:
            self._inbox = asyncio.Queue()
        return self._inbox

    def _deliver(self, messages) -> None:
        for m in messages:
            self._q().put_nowait(m)

    def inject(self, messages) -> None:
        """Deliver an autonomous (CLI-initiated) turn: no ``query()`` behind it."""
        self._deliver(messages)

    async def __aenter__(self):
        self._deliver(self._startup_frames)
        return self
    async def __aexit__(self, *exc):
        self._q().put_nowait(_EOF)
        return False
    def end_stream(self) -> None:
        """The stream ends with NOBODY closing the client: the CLI exited on its own,
        or its stdout reader died between turns. The driver must read this as a loss,
        not as the EOF its own ``aclose()`` produces."""
        self._q().put_nowait(_EOF)
    async def query(self, prompt: str):
        self.sent.append(prompt)
        self._deliver(self._messages)
        # A script with no ResultMessage (e.g. `[]`) still describes ONE turn that
        # ends — like the CLI, close it with a result so the driver's pump settles.
        if not self._messages or not isinstance(self._messages[-1], ResultMessage):
            self._deliver([result(uuid="script-end")])
    async def receive_messages(self):
        while True:
            m = await self._q().get()
            if m is _EOF:
                return
            # "Mid-turn" = the consumer has seen this turn's first message but not yet
            # its result; what interrupt() closes with a synthetic result. Bookkeeping
            # frames (hook lifecycle, task notices) neither open nor close a turn.
            if is_turn_message(m):
                self._turn_open = not isinstance(m, ResultMessage)
            yield m
    async def interrupt(self):
        self.interrupted = True
        q = self._q()
        rest = []
        while not q.empty():
            rest.append(q.get_nowait())
        if self._turn_open:
            q.put_nowait(result(uuid="interrupted"))
            self._turn_open = False
        if any(m is _EOF for m in rest):
            q.put_nowait(_EOF)
    async def stop_task(self, task_id: str) -> None:
        """Record the stop. The real CLI answers on the stream (killed/stopped notices);
        a test that needs them injects ``task_updated`` / ``task_notification`` itself."""
        self.stopped_tasks.append(task_id)
    async def set_permission_mode(self, mode: str): self.permission_mode = mode
    async def set_model(self, model=None): self.model = model
    async def get_server_info(self): return self._server_info
    async def get_context_usage(self): return self._context_usage
    async def get_mcp_status(self):
        v = self._mcp_status() if callable(self._mcp_status) else self._mcp_status
        if v is None:
            raise RuntimeError("mcp_status not scripted")   # mirrors an old CLI rejecting the control request
        return {"mcpServers": list(v)}
    async def reconnect_mcp_server(self, server_name):
        self.reconnected.append(server_name)
    async def toggle_mcp_server(self, server_name, enabled):
        if self.toggle_error is not None:
            raise self.toggle_error
        self.toggled.append((server_name, enabled))


class PerQueryScript:
    """Adapter for inline test fakes that script ONE turn per ``receive_response()``.

    The driver consumes the real client's persistent ``receive_messages()``; a fake
    written as "``receive_response()`` yields this turn" gets that stream by
    subclassing this: ``receive_response()`` is replayed once per ``query()`` call
    (the subclass's own ``query`` is wrapped at class creation, so it keeps whatever
    bookkeeping it does), and a script that ends without a ResultMessage is closed
    with a synthetic one — the turn boundary the driver keys on.
    """
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        orig = getattr(cls, "query", None)   # own or inherited (e.g. from FakeSDKClient)
        if orig is None or getattr(orig, "_pq_wrapped", False):
            return

        async def query(self, *a, **k):
            r = await orig(self, *a, **k)
            self._pq_signal().set()
            return r
        query._pq_wrapped = True   # type: ignore[attr-defined]
        cls.query = query

    def _pq_signal(self) -> "asyncio.Event":
        sig = self.__dict__.get("_pq_sig")
        if sig is None:
            sig = self.__dict__["_pq_sig"] = asyncio.Event()
        return sig

    async def receive_messages(self):
        while True:
            sig = self._pq_signal()
            await sig.wait()
            sig.clear()
            last = None
            async for m in self.receive_response():
                last = m
                yield m
            if not isinstance(last, ResultMessage):
                yield result(uuid="pq-end")
