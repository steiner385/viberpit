"""Fault-injecting, scriptable SDK stream source for driver/translator seam tests.

Where ``fake_sdk.FakeSDKClient`` yields a pre-built message list synchronously — so it
cannot express timing, interleaving, duplication, or a live event arriving while another
consumer replays — ``ScriptedSDKClient`` drives a *scenario* through ``receive_response()``
with deterministic gates and loop-yield delays. This is the primitive for testing the
attach-mid-turn races that every historical transcript regression lived in.

Scenario DSL
============

A scenario is a flat ``list`` of **steps**. Each step is one of:

* an SDK **message** — any object the ``fake_sdk`` builders produce (``sys_init``,
  ``stream_start``, ``stream_text``/``stream_thinking``, ``tool_use``,
  ``user_tool_result``, ``sys_compact_boundary``, ``assistant``, ``result``, …).
  These are REAL ``claude_agent_sdk`` objects — the same ones the live CLI emits — so
  the translator/driver see production-shaped input.
* a :class:`Gate` — a checkpoint. When the stream reaches a gate it *pauses* (signalling
  ``reached``) and resumes only when a test calls ``gate.release()``. This is the key
  primitive for attach-mid-turn races: drive a turn to a gate, attach a second
  subscriber, assert the coalesced snapshot, then release and assert the live tail.
* a :class:`Delay` — a deterministic inter-message pause. It yields control to the event
  loop ``ticks`` times (via ``asyncio.sleep(0)``) so other coroutines can interleave —
  NO wall-clock sleep, so tests stay fast and reproducible.

``receive_messages()`` is ONE persistent stream, exactly like the real SDK: each
``query()`` streams the next turn's steps until (and including) its ``ResultMessage``,
then the stream parks until the next query. A multi-turn scenario therefore drains
turn-by-turn as the driver dequeues prompts. Keep ONE client instance across a session's
turns (see :func:`scripted_factory`).

Composable builders
-------------------

Scenarios are plain code — concatenated ``list``s. Fragment builders return a list of
messages (no ``result``); :func:`turn` wraps fragments with a ``stream_start`` head and a
terminal ``result``. Fragments may be interleaved with ``Gate``/``Delay`` objects:

    scenario = (
        [f.sys_init("sess-1", model="claude-opus-4-8")]
        + turn("t1", say("t1", "Hello world"))
        + turn("t2", use_tool("t2", "tc1", "Read", {"file_path": "a.py"}, "print(1)\\n"),
                     say("t2", "Done"))
        + turn("t3", subagent_thread("t3", "task1", "sa1", "Explore", "searched"),
                     compaction(), say("t3", "wrapped up"))
    )

Later tasks (multi-client WS harness, cursor work) can reuse this DSL directly.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable

from claude_agent_sdk import ResultMessage, TextBlock

from . import fake_sdk as f


# ---- control steps ------------------------------------------------------------
class Gate:
    """A pause checkpoint in a scenario.

    The stream blocks when it reaches the gate (setting :attr:`is_reached`) and only
    continues when a test calls :meth:`release`. Both events are lazily-usable
    ``asyncio.Event``s (safe to construct outside a running loop on 3.10+).
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._reached = asyncio.Event()
        self._released = asyncio.Event()

    @property
    def is_reached(self) -> bool:
        return self._reached.is_set()

    async def wait_reached(self, timeout: float = 1.0) -> None:
        """Await until the stream has emitted everything BEFORE this gate.

        Deterministic replacement for ``sleep``-polling: when this returns, every
        message preceding the gate has been fed to the translator and emitted to the
        driver's subscribers/buffer (the stream is suspended inside the gate)."""
        await asyncio.wait_for(self._reached.wait(), timeout)

    def release(self) -> None:
        """Let the stream continue past this gate. Idempotent."""
        self._released.set()

    async def _arrive(self) -> None:
        self._reached.set()
        await self._released.wait()


class Delay:
    """A deterministic inter-message pause — ``ticks`` event-loop yields, no wall clock.

    Each tick (``asyncio.sleep(0)``) hands control to any other ready coroutine, so a
    ``Delay`` lets a concurrently-attaching subscriber interleave at a precise point in
    the stream without introducing real-time flakiness."""

    def __init__(self, ticks: int = 1) -> None:
        self.ticks = ticks

    async def _apply(self) -> None:
        for _ in range(self.ticks):
            await asyncio.sleep(0)


# ---- scripted client ----------------------------------------------------------
class ScriptedSDKClient:
    """Implements ``server.sessions.sdk_contract.SDKClientProtocol`` over a scenario.

    Unlike ``FakeSDKClient`` (whole message list per ``query``), this walks a persistent
    cursor across the scenario and streams ONE turn per ``query()`` — everything up to
    and including the next ``ResultMessage`` — so a multi-turn scenario drains
    turn-by-turn through the driver. ``Gate``/``Delay`` steps are honoured inline.
    """

    def __init__(self, steps: Iterable[Any], *, on_can_use_tool=None,
                 server_info=None, context_usage=None) -> None:
        self._steps = list(steps)
        self._pos = 0
        self._queries = 0          # query() calls so far
        self._served = 0           # turns the stream has started streaming
        self._woken: asyncio.Future | None = None
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.model = None
        self.sent: list[str] = []
        self._server_info = server_info
        self._context_usage = context_usage or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.sent.append(prompt)
        self._queries += 1
        self._wake()

    def _wake(self) -> None:
        if self._woken is not None and not self._woken.done():
            self._woken.set_result(None)

    async def _next_query(self) -> None:
        """Park until a ``query()`` opens the next turn (one persistent stream, like the
        real client: the next turn's steps are NOT streamed until a prompt asks)."""
        while self._served >= self._queries:
            self._woken = asyncio.get_running_loop().create_future()
            await self._woken
        self._served += 1

    async def receive_messages(self):
        """The persistent stream: one turn's steps per ``query()``, honouring gates and
        delays, each turn ending with its ResultMessage. Steps after the scenario's last
        result are never reached — the stream parks until the next query."""
        while self._pos < len(self._steps):
            await self._next_query()
            while self._pos < len(self._steps):
                if self.interrupted:
                    # Like the CLI: abandon the rest of this turn (skip to just past its
                    # scripted result) and flush a synthetic interrupted result.
                    self.interrupted = False
                    self._skip_turn()
                    yield f.result(uuid="interrupted")
                    break
                step = self._steps[self._pos]
                self._pos += 1
                if isinstance(step, Gate):
                    await step._arrive()
                    continue
                if isinstance(step, Delay):
                    await step._apply()
                    continue
                yield step
                if isinstance(step, ResultMessage):
                    break   # turn boundary — the next query resumes after it

    def _skip_turn(self) -> None:
        while self._pos < len(self._steps):
            step = self._steps[self._pos]
            self._pos += 1
            if isinstance(step, ResultMessage):
                return

    async def interrupt(self):
        self.interrupted = True
        self._wake()

    async def set_permission_mode(self, mode: str):
        self.permission_mode = mode

    async def set_model(self, model=None):
        self.model = model

    async def get_server_info(self):
        return self._server_info

    async def get_context_usage(self):
        return self._context_usage


def scripted_factory(steps, **client_kw):
    """Build an ``sdk_factory`` returning a single ``ScriptedSDKClient`` for ``steps``.

    Signature matches what ``SessionDriver`` calls:
    ``factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None)``.
    The SAME client instance is returned across turns so its cross-turn cursor advances
    (the driver builds the client once, in ``start()``)."""
    client = ScriptedSDKClient(steps, **client_kw)

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return client

    return factory


# ---- composable scenario builders --------------------------------------------
def _chunk(text: str, n: int) -> list[str]:
    """Split ``text`` into ``n`` roughly-equal, non-empty streamed pieces."""
    if n <= 1 or len(text) <= 1:
        return [text]
    n = min(n, len(text))
    size = -(-len(text) // n)   # ceil
    return [text[i:i + size] for i in range(0, len(text), size)]


def say(turn_id, text, *, chunks=2, model="claude", pid=None, session_id="sess-1"):
    """Streamed assistant text: N ``text_delta``s then the consolidated ``AssistantMessage``.

    ``pid`` (parent_tool_use_id) tags the fragment as a sub-agent thread's output."""
    out = [f.stream_text(p, uuid=turn_id, sid=session_id, pid=pid) for p in _chunk(text, chunks)]
    out.append(f.assistant(TextBlock(text=text), uuid=turn_id, parent_tool_use_id=pid, model=model))
    return out


def think(turn_id, text, *, chunks=1, pid=None, session_id="sess-1"):
    """Streamed thinking deltas (no consolidated message — mirrors the live stream)."""
    return [f.stream_thinking(p, uuid=turn_id, sid=session_id, pid=pid) for p in _chunk(text, chunks)]


def use_tool(turn_id, tool_id, name, inp, result_content, *, is_error=False, pid=None):
    """A tool round-trip: the ``tool_use`` (assistant) then its ``tool_result`` (user)."""
    return [f.tool_use(tool_id, name, inp, uuid=turn_id, pid=pid),
            f.user_tool_result(tool_id, result_content, is_error=is_error, pid=pid)]


def subagent_thread(parent_turn_id, task_id, sub_turn_id, subagent_type, text,
                    *, tools=(), session_id="sess-1"):
    """A ``Task`` sub-agent thread.

    Emits the parent's ``Task`` tool_use (main thread), then the sub-agent's own
    ``stream_start`` + streamed text (+ optional ``tools``) all tagged with
    ``parent_tool_use_id=task_id``, and finally the parent's ``Task`` tool_result.
    ``tools`` is an iterable of ``(tool_id, name, input, result_content)`` tuples."""
    out = [f.tool_use(task_id, "Task", {"subagent_type": subagent_type, "description": text[:20]},
                      uuid=parent_turn_id)]
    out.append(f.stream_start(uuid=sub_turn_id, sid=session_id, pid=task_id))
    out.extend(say(sub_turn_id, text, pid=task_id, session_id=session_id))
    for tool_id, name, inp, res in tools:
        out.extend(use_tool(sub_turn_id, tool_id, name, inp, res, pid=task_id))
    out.append(f.user_tool_result(task_id, f"[{subagent_type}] done"))
    return out


def compaction(trigger="auto", pre_tokens=1234):
    """A context-compaction boundary (drives a ``Condensing(active=False)`` event)."""
    return [f.sys_compact_boundary(trigger=trigger, pre_tokens=pre_tokens)]


def turn(turn_id, *fragments, session_id="sess-1", **result_kw):
    """Wrap content fragments as one complete turn.

    Prepends a ``stream_start`` (opens the turn) and appends a terminal ``ResultMessage``
    (closes it → the ``receive_response`` segment boundary). Each fragment is either a
    ``list`` of messages (concatenated in order) or a single step object — including a
    ``Gate``/``Delay`` for a mid-turn checkpoint. ``result_kw`` is forwarded to
    ``fake_sdk.result`` (e.g. ``subtype``, ``is_error``, ``usage``, ``cost``)."""
    steps: list[Any] = [f.stream_start(uuid=turn_id, sid=session_id)]
    for frag in fragments:
        if isinstance(frag, (list, tuple)):
            steps.extend(frag)
        else:
            steps.append(frag)
    steps.append(f.result(session_id=session_id, uuid=turn_id, **result_kw))
    return steps
