# Sub-Agent Thread Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render each `Task` sub-agent as a nested, collapsible thread attached to the Task card that spawned it, instead of flattening sub-agent activity into the main conversation thread.

**Architecture:** The `claude-agent-sdk` tags every streamed message with `parent_tool_use_id` (`None` = main thread; a `Task` tool-use id = that sub-agent's events). The backend translator currently ignores this field and stamps every event with the main turn id. We thread `parent_tool_use_id` onto the six block-bearing wire events, give each sub-agent its own turn in the translator, route tagged events into a per-sub-agent sub-thread in the frontend reducer, and render that sub-thread inside the spawning Task card. The linkage is free: a sub-agent's `parentToolUseId` already equals the `toolId` of its Task block.

**Tech Stack:** Python (FastAPI, `claude-agent-sdk`), pytest · React + TypeScript, Vitest + @testing-library/react.

## Global Constraints

- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit (constitution Principle V).
- Single source of truth / no dead code (Principle VI).
- This change is wire + UI state only — no SQLAlchemy model/column changes.
- New event fields MUST default to `None`/optional so existing call sites and replay stay valid.
- Backend tests run with the **main checkout's** prebuilt venv (this worktree has none), from the worktree root so `import server` resolves to the worktree:
  `PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python`
- Frontend tooling needs `web/node_modules`; symlink it from the main checkout (see Preflight).
- Wire field name is `parentToolUseId` (camelCase) on the wire/TS side, `parent_tool_use_id` (snake_case) on the Python side.

---

## Environment Preflight (run once before Task 1)

- [ ] **Symlink node_modules into the worktree** (no-op if already present)

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
ln -sfn /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/web/node_modules web/node_modules
ls web/node_modules/.bin/vitest && echo "node_modules OK"
```

- [ ] **Confirm the backend test runner sees the worktree's code**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
$PY -c "import server; print(server.__file__)"
# Expected: .../worktrees/cc-account-switcher/a4e74840/server/__init__.py
```

---

## Task 1: Backend — add `parent_tool_use_id` to block-bearing wire events

**Files:**
- Modify: `server/sessions/events.py` (dataclasses `TurnStart`, `TurnEnd`, `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`)
- Test: `server/tests/test_events.py`

**Interfaces:**
- Produces: each of the six dataclasses gains a trailing field `parent_tool_use_id: str | None = None`, and its `to_wire()` dict gains `"parentToolUseId": self.parent_tool_use_id`. Field order (positional construction stays valid):
  - `TurnStart(turn_id, src_uuid=None, parent_tool_use_id=None)`
  - `TurnEnd(turn_id, usage=None, cost_usd=None, src_uuid=None, parent_tool_use_id=None)`
  - `TextDelta(turn_id, text, src_uuid=None, parent_tool_use_id=None)`
  - `ThinkingDelta(turn_id, text, src_uuid=None, parent_tool_use_id=None)`
  - `ToolCall(turn_id, tool_id, name, kind, input, src_uuid=None, parent_tool_use_id=None)`
  - `ToolResult(tool_id, ok, payload, src_uuid=None, parent_tool_use_id=None)`

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_events.py`:

```python
from server.sessions.events import (TurnStart, TurnEnd, TextDelta, ThinkingDelta,
                                     ToolCall, ToolResult, ToolKind)

def test_block_events_carry_parent_tool_use_id_on_wire():
    tc = ToolCall("t1", "g1", "Grep", ToolKind.SEARCH, {}, src_uuid="u1",
                  parent_tool_use_id="task1")
    assert tc.to_wire()["parentToolUseId"] == "task1"

    td = TextDelta("t1", "hi", src_uuid="u1", parent_tool_use_id="task1")
    assert td.to_wire()["parentToolUseId"] == "task1"

    tr = ToolResult("g1", True, {"content": "x"}, src_uuid="u1", parent_tool_use_id="task1")
    assert tr.to_wire()["parentToolUseId"] == "task1"

def test_block_events_default_parent_to_none_on_wire():
    for e in (TurnStart("t1"), TurnEnd("t1"), ThinkingDelta("t1", "z")):
        assert e.to_wire()["parentToolUseId"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
$PY -m pytest server/tests/test_events.py -k parent_tool_use_id -v
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'parent_tool_use_id'`.

- [ ] **Step 3: Add the field + wire key to each of the six dataclasses**

In `server/sessions/events.py`, edit each dataclass. Final forms:

```python
@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id}

@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class ThinkingDelta:
    turn_id: str; text: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "thinkingDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class ToolCall:
    turn_id: str; tool_id: str; name: str; kind: ToolKind; input: dict
    src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "toolCall", "turnId": self.turn_id, "toolId": self.tool_id,
                               "name": self.name, "toolKind": self.kind.value, "input": self.input,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class ToolResult:
    tool_id: str; ok: bool; payload: dict; src_uuid: str | None = None
    parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "toolResult", "toolId": self.tool_id, "ok": self.ok,
                               "payload": self.payload, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id}
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
$PY -m pytest server/tests/test_events.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events.py
git commit -m "feat(events): add parentToolUseId to block-bearing session events

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Backend — per-agent turns + `parent_tool_use_id` stamping in the translator

**Files:**
- Modify: `server/sessions/translator.py`
- Modify: `server/tests/fakes/fake_sdk.py` (add `pid` param to builders)
- Test: `server/tests/test_translator.py`

**Interfaces:**
- Consumes: the six events from Task 1 (with `parent_tool_use_id` kwarg).
- Produces: `Translator.feed(msg)` stamps every emitted event's `parent_tool_use_id` from `getattr(msg, "parent_tool_use_id", None)`. Each distinct `parent_tool_use_id` (including `None` for main) gets its own `TurnStart` and turn id. `Translator` keeps `self._turn_id` as the **main-thread** turn id (so `SessionDriver._turn()` keeps working unchanged).
- `fake_sdk` builders `stream_start`, `stream_text`, `stream_thinking`, `assistant`, `tool_use`, `user_tool_result` gain a trailing `pid: str | None = None` keyword that sets `parent_tool_use_id` on the constructed SDK object.

- [ ] **Step 1: Add `pid` to the fake_sdk builders**

In `server/tests/fakes/fake_sdk.py`, update the six builders (the SDK dataclasses already accept `parent_tool_use_id`, default `None`):

```python
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

def assistant(*blocks, uuid: str = "u1", pid: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude", uuid=uuid, parent_tool_use_id=pid)

def tool_use(tool_id: str, name: str, inp: dict, uuid: str = "u1", pid: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=inp)],
                            model="claude", uuid=uuid, parent_tool_use_id=pid)

def user_tool_result(tool_id: str, content, is_error: bool = False, uuid: str = "u2",
                     pid: str | None = None) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)],
                       uuid=uuid, parent_tool_use_id=pid)
```

- [ ] **Step 2: Write the failing tests**

Add to `server/tests/test_translator.py` (extend the import on line 3 to include `TextDelta`):

```python
def test_subagent_events_tagged_and_separately_turned():
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("task1", "Task", {"subagent_type": "Explore", "description": "x"}, uuid="t1"),
            f.stream_start(uuid="sa1", pid="task1"),
            f.stream_text("sub-text", uuid="sa1", pid="task1"),
            f.tool_use("g1", "Grep", {"pattern": "foo"}, uuid="sa1", pid="task1"),
            f.result(uuid="t1")]
    _, out = _run(msgs)
    task = next(e for e in out if isinstance(e, ToolCall) and e.name == "Task")
    grep = next(e for e in out if isinstance(e, ToolCall) and e.name == "Grep")
    assert task.parent_tool_use_id is None
    assert grep.parent_tool_use_id == "task1"
    sub_text = next(e for e in out if isinstance(e, TextDelta) and e.parent_tool_use_id == "task1")
    main_start = next(e for e in out if isinstance(e, TurnStart) and e.parent_tool_use_id is None)
    assert sub_text.turn_id != main_start.turn_id

def test_parallel_subagents_get_distinct_turns():
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("taskA", "Task", {}, uuid="t1"),
            f.tool_use("taskB", "Task", {}, uuid="t1"),
            f.stream_start(uuid="saA", pid="taskA"), f.stream_text("A", uuid="saA", pid="taskA"),
            f.stream_start(uuid="saB", pid="taskB"), f.stream_text("B", uuid="saB", pid="taskB"),
            f.result(uuid="t1")]
    _, out = _run(msgs)
    starts = {e.parent_tool_use_id: e.turn_id for e in out if isinstance(e, TurnStart)}
    assert starts["taskA"] != starts["taskB"]
    assert starts[None] not in (starts["taskA"], starts["taskB"])
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
$PY -m pytest server/tests/test_translator.py -k "subagent or parallel" -v
```
Expected: FAIL — sub-agent events come back with `parent_tool_use_id is None` and share the main turn id.

- [ ] **Step 4: Rewrite the translator's turn state to be per-agent**

Replace the `__init__`, `_ensure_turn`, `_reset`, and `feed` bodies in `server/sessions/translator.py` with:

```python
class Translator:
    def __init__(self):
        self.session_id: str | None = None
        self._turn_ids: dict[str | None, str] = {}   # parent_tool_use_id -> turn id (None = main)
        self._open: set = set()                       # agent keys that emitted TurnStart
        self._turn_id: str = ""                       # mirror of main turn id (driver._turn compat)
        self._seen_tools: set[str] = set()

    def _ensure_turn(self, uuid, pid=None) -> list:
        if pid in self._open:
            return []
        tid = self._turn_ids.get(pid) or uuid or "turn"
        self._turn_ids[pid] = tid
        self._open.add(pid)
        if pid is None:
            self._turn_id = tid
        return [ev.TurnStart(tid, uuid, parent_tool_use_id=pid)]

    def _turn(self, pid=None) -> str:
        return self._turn_ids.get(pid) or "turn"

    def _reset(self):
        self._turn_ids = {}
        self._open = set()
        self._turn_id = ""
        self._seen_tools = set()

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model:
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
            return []

        if isinstance(msg, StreamEvent):
            pid = getattr(msg, "parent_tool_use_id", None)
            e = msg.event or {}
            t = e.get("type")
            if t == "message_start":
                return self._ensure_turn(msg.uuid, pid)
            if t == "content_block_delta":
                out = self._ensure_turn(msg.uuid, pid)
                tid = self._turn(pid)
                delta = e.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out.append(ev.TextDelta(tid, delta.get("text", ""), msg.uuid, parent_tool_use_id=pid))
                elif dt == "thinking_delta":
                    out.append(ev.ThinkingDelta(tid, delta.get("thinking", ""), msg.uuid, parent_tool_use_id=pid))
                return out
            return []

        if isinstance(msg, AssistantMessage):
            pid = getattr(msg, "parent_tool_use_id", None)
            out = self._ensure_turn(getattr(msg, "uuid", None), pid)
            tid = self._turn(pid)
            for b in msg.content:
                if isinstance(b, ToolUseBlock) and b.id not in self._seen_tools:
                    self._seen_tools.add(b.id)
                    if b.name == "AskUserQuestion":
                        continue
                    out.append(ev.ToolCall(tid, b.id, b.name, ev.tool_kind(b.name),
                                           b.input, getattr(msg, "uuid", None),
                                           parent_tool_use_id=pid))
            return out

        if isinstance(msg, UserMessage):
            pid = getattr(msg, "parent_tool_use_id", None)
            out = []
            for rb in msg.content:
                if isinstance(rb, ToolResultBlock):
                    out.append(ev.ToolResult(rb.tool_use_id, not bool(rb.is_error),
                                             {"content": rb.content}, getattr(msg, "uuid", None),
                                             parent_tool_use_id=pid))
            return out

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_ids.get(None) or "turn"
            self._reset()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None))]

        return []
```

- [ ] **Step 5: Run the full translator + driver suites to verify pass + no regression**

```bash
$PY -m pytest server/tests/test_translator.py server/tests/test_driver.py server/tests/test_driver_status.py -v
```
Expected: PASS (existing tests still green — main-thread behavior is unchanged because `pid` defaults to `None`).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/translator.py server/tests/fakes/fake_sdk.py server/tests/test_translator.py
git commit -m "feat(translator): give each sub-agent its own turn and tag events with parent_tool_use_id

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Backend — replay skips `isSidechain` transcript entries

**Files:**
- Modify: `server/sessions/replay.py` (`_parse`)
- Test: `server/tests/test_replay.py`

**Interfaces:**
- Produces: `_parse` ignores any transcript line whose top-level `isSidechain` is truthy, so older single-file (Layout A) transcripts can't leak sub-agent steps into the main thread on reattach.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_replay.py`:

```python
import json
from server.sessions.replay import _parse

def test_replay_skips_sidechain_entries(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "uuid": "m1",
         "message": {"model": "claude", "content": [{"type": "text", "text": "main-line"}]}},
        {"type": "assistant", "uuid": "s1", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "subagent-secret"}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    out = _parse(str(p), 20)
    texts = [getattr(e, "text", None) for e in out]
    assert "main-line" in texts
    assert "subagent-secret" not in texts
```

- [ ] **Step 2: Run test to verify it fails**

```bash
$PY -m pytest server/tests/test_replay.py -k sidechain -v
```
Expected: FAIL — `"subagent-secret"` appears in `texts`.

- [ ] **Step 3: Skip sidechain rows in `_parse`**

In `server/sessions/replay.py`, inside `for o in lines:` (immediately after the `for o in lines:` line, before `typ = o.get("type")`), add:

```python
        if o.get("isSidechain"):
            continue
```

- [ ] **Step 4: Run the test + full replay suite to verify pass**

```bash
$PY -m pytest server/tests/test_replay.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay.py
git commit -m "fix(replay): skip isSidechain transcript entries so sub-agent steps don't leak into the main thread

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — add `parentToolUseId` to the wire event types

**Files:**
- Modify: `web/src/session/events.ts`
- Test: `web/src/session/events.test.ts`

**Interfaces:**
- Produces: `EvTurnStart`, `EvTurnEnd`, `EvTextDelta`, `EvThinkingDelta`, `EvToolCall`, `EvToolResult` each gain `parentToolUseId?: string | null`.

- [ ] **Step 1: Write the failing test**

Add to `web/src/session/events.test.ts`:

```typescript
import type { EvToolCall, EvTextDelta } from './events'

describe('parentToolUseId field', () => {
  it('is carried on EvToolCall and EvTextDelta', () => {
    const tc: EvToolCall = {
      type: 'toolCall', turnId: 't', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    }
    const td: EvTextDelta = { type: 'textDelta', turnId: 't', text: 'hi', parentToolUseId: 'task1' }
    expect(tc.parentToolUseId).toBe('task1')
    expect(td.parentToolUseId).toBe('task1')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx tsc --noEmit
```
Expected: FAIL — `Object literal may only specify known properties, and 'parentToolUseId' does not exist in type 'EvToolCall'`.

- [ ] **Step 3: Add the optional field to the six interfaces**

In `web/src/session/events.ts`, edit lines 5–10:

```typescript
export interface EvTurnStart     { type: 'turnStart';     turnId: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTurnEnd       { type: 'turnEnd';       turnId: string; usage?: Record<string, unknown> | null; costUsd?: number | null; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTextDelta     { type: 'textDelta';     turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvThinkingDelta { type: 'thinkingDelta'; turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvToolCall      { type: 'toolCall';      turnId: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvToolResult    { type: 'toolResult';    toolId: string; ok: boolean; payload: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null }
```

- [ ] **Step 4: Run typecheck + the test to verify pass**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx tsc --noEmit && npx vitest run src/session/events.test.ts
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
git add web/src/session/events.ts web/src/session/events.test.ts
git commit -m "feat(web/events): add optional parentToolUseId to block-bearing event types

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — route tagged events into a per-sub-agent thread in the reducer

**Files:**
- Modify: `web/src/session/reducer.ts`
- Test: `web/src/session/reducer.test.ts`

**Interfaces:**
- Consumes: `EvUser | EvTurnStart | EvTextDelta | EvThinkingDelta | EvToolCall | EvToolResult` (the block-bearing events), now optionally carrying `parentToolUseId`.
- Produces:
  - `export interface SubThread { turns: Turn[]; openBlockId: string | null }`
  - `TranscriptState` gains `subAgents: Record<string, SubThread>` (key = the Task `toolId`); `initialTranscript.subAgents = {}`.
  - Block-bearing events with a truthy `parentToolUseId` are applied to `state.subAgents[parentToolUseId]` (created on demand); all other events behave exactly as before on the main thread.

- [ ] **Step 1: Write the failing tests**

Add to `web/src/session/reducer.test.ts` (import `initialTranscript`, `transcriptReducer` if not already imported at the top of that file):

```typescript
import { transcriptReducer, initialTranscript } from './reducer'

describe('sub-agent thread routing', () => {
  it('routes a tagged toolCall into subAgents, leaving main turns empty', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'sa1', parentToolUseId: 'task1' })
    s = transcriptReducer(s, {
      type: 'toolCall', turnId: 'sa1', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    })
    expect(s.turns).toEqual([])
    expect(s.subAgents['task1'].turns[0].blocks[0]).toMatchObject({ kind: 'tool', toolId: 'g1' })
  })

  it('keeps untagged events on the main thread', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'hi' })
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hi' })
    expect(s.subAgents).toEqual({})
  })

  it('isolates open-text state per thread during concurrent streaming', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 't1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'main-' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 'sa1', text: 'sub', parentToolUseId: 'task1' })
    s = transcriptReducer(s, { type: 'textDelta', turnId: 't1', text: 'continued' })
    const mainText = s.turns[0].blocks
      .filter((b) => b.kind === 'text').map((b) => (b as { text: string }).text).join('')
    expect(mainText).toBe('main-continued')
    expect(s.subAgents['task1'].turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'sub' })
  })

  it('routes a tagged toolResult into the matching sub-thread tool block', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'sa1', parentToolUseId: 'task1' })
    s = transcriptReducer(s, {
      type: 'toolCall', turnId: 'sa1', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    })
    s = transcriptReducer(s, {
      type: 'toolResult', toolId: 'g1', ok: true, payload: { content: 'hit' }, parentToolUseId: 'task1',
    })
    const block = s.subAgents['task1'].turns[0].blocks[0]
    expect(block).toMatchObject({ kind: 'tool', toolId: 'g1', result: { ok: true } })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx vitest run src/session/reducer.test.ts
```
Expected: FAIL — `s.subAgents` is undefined; tagged events land in `s.turns`.

- [ ] **Step 3: Add `SubThread`, the `subAgents` state field, and the extracted thread reducer**

In `web/src/session/reducer.ts`:

(a) After the `Turn` interface (line 14), add:

```typescript
/** A self-contained conversation thread (the main transcript, or one sub-agent). */
export interface SubThread { turns: Turn[]; openBlockId: string | null }
```

(b) In `TranscriptState`, add a field (after `queued: string[]`):

```typescript
  subAgents: Record<string, SubThread>   // keyed by the spawning Task toolId
```

(c) In `initialTranscript`, add:

```typescript
  subAgents: {},
```

(d) Add the imports for the block event types at the top (extend the existing `import type` from `./events`):

```typescript
import type {
  SessionEvent, ToolKind, EvUser, EvTurnStart, EvTextDelta, EvThinkingDelta, EvToolCall, EvToolResult,
} from './events'
```

(e) Add the thread-scoped reducer just above `transcriptReducer`. It contains the existing `user`/`turnStart`/`textDelta`/`thinkingDelta`/`toolCall`/`toolResult` logic, rewritten to operate on a `SubThread` and to return the *same* `thread` reference on a no-op (so the caller can skip a state update):

```typescript
type BlockEvent = EvUser | EvTurnStart | EvTextDelta | EvThinkingDelta | EvToolCall | EvToolResult

const BLOCK_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user', 'turnStart', 'textDelta', 'thinkingDelta', 'toolCall', 'toolResult',
])

/** Apply a block-bearing event to a single thread. Returns the same `thread` reference on a no-op. */
function applyToThread(thread: SubThread, ev: BlockEvent): SubThread {
  switch (ev.type) {
    case 'user': {
      const { turnId, text } = ev
      const userBlock: Block = { kind: 'text', id: `${turnId}:u`, text }
      const idx = findTurnIdx(thread.turns, turnId)
      const turns =
        idx === -1
          ? [...thread.turns, { id: turnId, role: 'user' as const, blocks: [userBlock] }]
          : replaceTurn(thread.turns, idx, (t) => ({
              ...t,
              blocks: t.blocks.map((b) => (b.kind === 'text' && b.id === `${turnId}:u` ? userBlock : b)),
            }))
      return { turns, openBlockId: null }
    }

    case 'turnStart': {
      const idx = findTurnIdx(thread.turns, ev.turnId)
      if (idx !== -1) {
        if (thread.openBlockId === null) return thread
        return { turns: thread.turns, openBlockId: null }
      }
      return {
        turns: [...thread.turns, { id: ev.turnId, role: 'assistant', blocks: [] }],
        openBlockId: null,
      }
    }

    case 'textDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      const openBlock = thread.openBlockId
        ? turn.blocks.find((b) => b.id === thread.openBlockId && b.kind === 'text')
        : undefined
      if (openBlock && openBlock.kind === 'text') {
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'text' ? { ...b, text: b.text + text } : b),
        }))
        return { turns: nextTurns, openBlockId: thread.openBlockId }
      }
      const blockId = `${turnId}:t${turn.blocks.length}`
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t, blocks: [...t.blocks, { kind: 'text', id: blockId, text }],
      }))
      return { turns: nextTurns, openBlockId: blockId }
    }

    case 'thinkingDelta': {
      const { turnId, text } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      const openBlock = thread.openBlockId
        ? turn.blocks.find((b) => b.id === thread.openBlockId && b.kind === 'thinking')
        : undefined
      if (openBlock && openBlock.kind === 'thinking') {
        const nextTurns = replaceTurn(turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) =>
            b.id === openBlock.id && b.kind === 'thinking' ? { ...b, text: b.text + text } : b),
        }))
        return { turns: nextTurns, openBlockId: thread.openBlockId }
      }
      const blockId = `${turnId}:th${turn.blocks.length}`
      const nextTurns = replaceTurn(turns, idx, (t) => ({
        ...t, blocks: [...t.blocks, { kind: 'thinking', id: blockId, text }],
      }))
      return { turns: nextTurns, openBlockId: blockId }
    }

    case 'toolCall': {
      const { turnId, toolId, name, toolKind, input } = ev
      const [turns, idx] = ensureAssistantTurn(thread.turns, turnId)
      const turn = turns[idx]
      if (turn.blocks.some((b) => b.kind === 'tool' && b.toolId === toolId)) {
        if (thread.openBlockId === null && turns === thread.turns) return thread
        return { turns, openBlockId: null }
      }
      const newBlock: Block = { kind: 'tool', id: toolId, toolId, name, toolKind, input }
      const nextTurns = replaceTurn(turns, idx, (t) => ({ ...t, blocks: [...t.blocks, newBlock] }))
      return { turns: nextTurns, openBlockId: null }
    }

    case 'toolResult': {
      const { toolId, ok, payload } = ev
      let found = false
      const nextTurns = thread.turns.map((turn) => {
        const blockIdx = turn.blocks.findIndex((b) => b.kind === 'tool' && b.toolId === toolId)
        if (blockIdx === -1) return turn
        found = true
        return {
          ...turn,
          blocks: turn.blocks.map((b, i) =>
            i === blockIdx && b.kind === 'tool' ? { ...b, result: { ok, payload } } : b),
        }
      })
      if (!found) return thread
      return { turns: nextTurns, openBlockId: thread.openBlockId }
    }
  }
}
```

(f) At the very top of `transcriptReducer` (before the existing `switch`), insert the routing layer, and **delete the now-moved `user` / `turnStart` / `textDelta` / `thinkingDelta` / `toolCall` / `toolResult` cases from the switch** (they are handled by `applyToThread`):

```typescript
export function transcriptReducer(
  state: TranscriptState,
  ev: SessionEvent | LocalAction,
): TranscriptState {
  // Route block-bearing events to a thread: the spawning Task's sub-thread when
  // tagged with parentToolUseId, otherwise the main transcript.
  if (BLOCK_EVENT_TYPES.has(ev.type)) {
    const pid = (ev as { parentToolUseId?: string | null }).parentToolUseId
    if (pid) {
      const cur = state.subAgents[pid] ?? { turns: [], openBlockId: null }
      const next = applyToThread(cur, ev as BlockEvent)
      if (next === cur) return state
      return { ...state, subAgents: { ...state.subAgents, [pid]: next } }
    }
    const cur: SubThread = { turns: state.turns, openBlockId: state.openBlockId }
    const next = applyToThread(cur, ev as BlockEvent)
    if (next.turns === state.turns && next.openBlockId === state.openBlockId) return state
    return { ...state, turns: next.turns, openBlockId: next.openBlockId }
  }

  switch (ev.type) {
    // ── permissionRequest / question / sessionMeta / title / status / turnEnd /
    //    replayComplete / error / queueUpdate / _resolvePermission / _resolveQuestion ──
    //    (unchanged — leave these existing cases in place)
```

Leave the remaining (non-block) cases and the `default` exactly as they are.

- [ ] **Step 4: Run the reducer tests + typecheck to verify pass + no regression**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx tsc --noEmit && npx vitest run src/session/reducer.test.ts src/session/reducer.meta.test.ts
```
Expected: PASS (existing reducer tests stay green — untagged events take the main-thread path with identical behavior).

- [ ] **Step 5: Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
git add web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(web/reducer): route sub-agent events into a per-Task sub-thread

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — render the sub-agent thread inside its Task card

**Files:**
- Modify: `web/src/components/session/ToolCallCard.tsx` (nested transcript for `name === 'Task'`)
- Modify: `web/src/components/session/Turn.tsx` (accept + forward a `subAgents` map)
- Modify: `web/src/components/session/TranscriptView.tsx` (accept + forward `subAgents`)
- Modify: `web/src/components/session/SessionView.tsx` (pass `state.subAgents`)
- Test: `web/src/components/__tests__/ToolCallCard.subagent.test.tsx` (create)

**Interfaces:**
- Consumes: `state.subAgents` (`Record<string, SubThread>`) from Task 5; the existing `Turn` component for rendering nested blocks.
- Produces:
  - `ToolCallCard` gains a prop `subThread?: SubThread`. When `block.name === 'Task'` and `subThread` has turns, the card renders a collapsible "Sub-agent activity" region containing the sub-thread's blocks (reusing `Turn`). Collapsed by default.
  - `Turn` gains a prop `subAgents?: Record<string, SubThread>` and passes `subAgents?.[block.toolId]` as `subThread` to each `ToolCallCard`.
  - `TranscriptView` gains a prop `subAgents: Record<string, SubThread>` and forwards it to every `Turn`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/ToolCallCard.subagent.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToolCallCard } from '../session/ToolCallCard'
import type { Block, SubThread } from '@/session/reducer'

const taskBlock: Extract<Block, { kind: 'tool' }> = {
  kind: 'tool', id: 'task1', toolId: 'task1', name: 'Task', toolKind: 'unknown',
  input: { subagent_type: 'Explore', description: 'find the bug' },
}

const subThread: SubThread = {
  openBlockId: null,
  turns: [{ id: 'sa1', role: 'assistant', blocks: [
    { kind: 'text', id: 'sa1:t0', text: 'sub-agent says hello' },
  ] }],
}

describe('ToolCallCard sub-agent thread', () => {
  it('renders the nested sub-agent transcript when expanded', async () => {
    render(<ToolCallCard block={taskBlock} subThread={subThread} />)
    // Collapsed by default: nested text not visible yet.
    expect(screen.queryByText('sub-agent says hello')).not.toBeInTheDocument()
    // Expand the sub-agent region.
    await userEvent.click(screen.getByRole('button', { name: /sub-agent activity/i }))
    expect(screen.getByText('sub-agent says hello')).toBeInTheDocument()
  })

  it('does not render a sub-agent region for a non-Task tool', () => {
    const bash: Extract<Block, { kind: 'tool' }> = {
      kind: 'tool', id: 'b1', toolId: 'b1', name: 'Bash', toolKind: 'bash', input: { command: 'ls' },
    }
    render(<ToolCallCard block={bash} />)
    expect(screen.queryByRole('button', { name: /sub-agent activity/i })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx vitest run src/components/__tests__/ToolCallCard.subagent.test.tsx
```
Expected: FAIL — `ToolCallCard` has no `subThread` prop and renders no "Sub-agent activity" button.

- [ ] **Step 3: Add the nested sub-agent region to `ToolCallCard`**

In `web/src/components/session/ToolCallCard.tsx`:

(a) Update imports (add `SubThread` type and the `Turn` component; note `Turn` imports `ToolCallCard`, so to avoid a circular-import hazard at module eval, import it lazily inside the component is unnecessary — Vite/ESM handles the cycle since both are function components referenced at render time):

```typescript
import type { Block, SubThread } from '@/session/reducer'
import { Turn } from './Turn'
```

(b) Extend `Props`:

```typescript
interface Props {
  block: ToolBlock
  defaultResultOpen?: boolean
  /** When this is a Task tool, the spawned sub-agent's own thread (rendered nested). */
  subThread?: SubThread
}
```

(c) In the component signature, destructure `subThread`, and add a disclosure state + render the region just before the closing `</div>` of the card (after the result section):

```typescript
export function ToolCallCard({ block, defaultResultOpen = false, subThread }: Props) {
  const [argsOpen, setArgsOpen] = useState(false)
  const [resultOpen, setResultOpen] = useState(defaultResultOpen)
  const [subOpen, setSubOpen] = useState(false)
  const argsRegionId = useId()
  const resultRegionId = useId()
  const subRegionId = useId()
  // ...existing summary + return JSX unchanged, except add the block below
  //    immediately before the final closing </div>:
```

Add this block as the last child inside the outermost `<div className="my-1 rounded ...">`, after the result section IIFE:

```tsx
      {/* Sub-agent thread (Task tool only) */}
      {block.name === 'Task' && subThread && subThread.turns.length > 0 && (
        <div className="border-t border-gray-200 dark:border-gray-700">
          <button
            type="button"
            aria-label="Sub-agent activity"
            aria-expanded={subOpen}
            aria-controls={subRegionId}
            onClick={() => setSubOpen((v) => !v)}
            className={cn(
              'flex w-full items-center gap-2 px-3 py-1 text-left min-h-[44px]',
              'text-indigo-700 dark:text-indigo-300',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            <span className="text-xs font-medium">Sub-agent activity</span>
            <span className="text-xs opacity-60">{subThread.turns.length} turn{subThread.turns.length === 1 ? '' : 's'}</span>
            <span className="ml-auto shrink-0 text-gray-400 dark:text-gray-500">
              {subOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          </button>
          <div
            id={subRegionId}
            hidden={!subOpen}
            className="border-t border-gray-200 dark:border-gray-700 bg-indigo-50/30 dark:bg-indigo-950/10 pl-2"
          >
            {subThread.turns.map((t) => (
              <Turn key={t.id} turn={t} permissionMode="interactive" onDecision={() => {}} onAnswer={() => {}} />
            ))}
          </div>
        </div>
      )}
```

(Note: sub-agent threads contain no permission/question blocks, so the no-op `onDecision`/`onAnswer` handlers are never invoked.)

- [ ] **Step 4: Run the component test to verify pass**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx vitest run src/components/__tests__/ToolCallCard.subagent.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Thread `subAgents` from SessionView → TranscriptView → Turn → ToolCallCard**

(a) `web/src/components/session/Turn.tsx` — add the prop and forward it:

```tsx
import type { Turn as TurnType, SubThread } from '@/session/reducer'

interface Props {
  turn: TurnType
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
  subAgents?: Record<string, SubThread>
}

export const Turn = memo(function Turn({ turn, permissionMode, onDecision, onAnswer, subAgents }: Props) {
  // ...unchanged, except the 'tool' case:
          case 'tool':
            return <ToolCallCard key={block.id} block={block} subThread={subAgents?.[block.toolId]} />
```

(b) `web/src/components/session/TranscriptView.tsx` — add the prop and forward it:

```tsx
import type { Turn as TurnType, SubThread } from '@/session/reducer'

interface Props {
  turns: TurnType[]
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
  subAgents: Record<string, SubThread>
}

export function TranscriptView({ turns, permissionMode, onDecision, onAnswer, subAgents }: Props) {
  // ...unchanged, except the <Turn> render:
        <Turn
          key={turn.id}
          turn={turn}
          permissionMode={permissionMode}
          onDecision={onDecision}
          onAnswer={onAnswer}
          subAgents={subAgents}
        />
```

(c) `web/src/components/session/SessionView.tsx` — pass `state.subAgents` (around line 283):

```tsx
      <TranscriptView
        turns={state.turns}
        permissionMode={mode}
        onDecision={handleDecision}
        onAnswer={handleAnswer}
        subAgents={state.subAgents}
      />
```

- [ ] **Step 6: Run typecheck, lint, and the full web suite to verify pass**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx tsc --noEmit && npx eslint src/components/session src/session && npx vitest run
```
Expected: PASS, no type or lint errors.

- [ ] **Step 7: Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
git add web/src/components/session/ToolCallCard.tsx web/src/components/session/Turn.tsx web/src/components/session/TranscriptView.tsx web/src/components/session/SessionView.tsx web/src/components/__tests__/ToolCallCard.subagent.test.tsx
git commit -m "feat(web): render each Task sub-agent as a nested collapsible thread

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] **Backend suite**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $PY -m pytest server/tests/ -q
```
Expected: all pass.

- [ ] **Frontend suite + types + lint**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/a4e74840/web
npx vitest run && npx tsc --noEmit && npx eslint .
```
Expected: all pass.

- [ ] **Open the PR** per the project completion workflow (branch `002-subagent-thread-rendering` → PR → merge once green).

---

## Self-Review notes (author)

- **Spec coverage:** §Backend → Tasks 1–2; §Frontend events → Task 4; §Frontend reducer (incl. per-thread `openBlockId` bug fix) → Task 5; §Frontend Task card → Task 6; §Scope boundary replay `isSidechain` skip → Task 3; §Testing → tests in every task. Follow-up (replay reconstruction from `subagents/*.jsonl`) is explicitly out of scope and has no task, by design.
- **Type consistency:** `SubThread` is defined in Task 5 and consumed by Task 6; `parentToolUseId` (wire/TS) ↔ `parent_tool_use_id` (Python) used consistently; `applyToThread` / `subAgents` names match across tasks.
- **No placeholders:** every code step shows complete code; every command has expected output.
