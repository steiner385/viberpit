# Sub-Agent Session Drill-Down Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop sub-agent (Task) responses/tool-uses from appearing in the parent's main conversation thread, and make each sub-agent reviewable as a hierarchical, live sub-session nested under its parent in the session list.

**Architecture:** Add one piece of identity — `agent_id` (the spawning Task tool's id; `null` = main agent) — and thread it through every event on both the live path (`Translator` reads the SDK's `parent_tool_use_id`) and the replay path (`replay.py` reads JSONL `isSidechain`/`parentUuid`). The frontend tags each `Turn` with its `agentId`, renders only the `null` bucket in the main thread, and renders the rest as nested sub-session rows that open read-only (with a breadcrumb) in the main pane. Sub-agents are derived projections of existing events — no new processes.

**Tech Stack:** Python 3 · FastAPI · SQLAlchemy (SQLite) · claude-agent-sdk · React + Vite + TypeScript · Tailwind · vitest · pytest.

## Global Constraints

- **Never fork/patch the Claude binary** (Constitution I). We only consume SDK messages and the JSONL transcript.
- **TDD + quality gates** (Constitution V): every task is red→green→commit. Gates: `pytest server/tests/`, and in `web/`: `vitest run`, `tsc --noEmit`, `eslint`.
- **Verify against the real binary, not assumptions** (Constitution III): the replay JSONL→sub-agent linkage MUST be pinned to a real captured transcript (Task 1).
- **New DB columns must be nullable** (CLAUDE.md): `init_db` backfills nullable columns; a NOT NULL column would need a real migration.
- **No dead code / single source of truth** (Constitution VI): share the live-stream wiring via one hook rather than duplicating it.
- **Test env**: a fresh worktree has no built env. Run server tests with the main checkout's prebuilt interpreter:
  `/mnt/ssk-ssd/tony/GitHub/KinDash/.venv/bin/python -m pytest …` is NOT correct — use the cc-account-switcher main checkout venv. Resolve it once: `MAIN=$(git -C /home/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9d1b66a5 rev-parse --show-toplevel 2>/dev/null)`. In practice run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/…`. For API auth tests use `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.
- **Wire naming**: backend emits camelCase keys in `to_wire()` (e.g. `agentId`), matching existing `srcUuid`/`turnId`.

---

## File Structure

**Backend (Python):**
- `server/sessions/events.py` — add `agent_id` to content events + new `SubAgentUpdate` event. (modify)
- `server/sessions/translator.py` — per-agent-context turn state; tag events; emit `SubAgentUpdate`. (modify)
- `server/sessions/replay.py` — tag replayed events with `agent_id`; emit `SubAgentUpdate` from JSONL. (modify)
- `server/sessions/subagents.py` — **new** pure helper: build the sub-agent tree/summary from a list of events. Shared by orchestrator + tests.
- `server/db/models.py` — nullable `sub_agents` JSON column on `Session`. (modify)
- `server/sessions/orchestrator.py` — expose `sub_agents(session_id)` summary (active: from live events; archived: from persisted column / replay). (modify)
- `server/api/sessions.py` — include `subAgents` in `_record_to_dict`. (modify)
- `server/tests/fixtures/transcript_subagent.jsonl` — **new** captured real fixture.
- `server/tests/test_subagents.py`, `server/tests/test_translator_subagent.py`, `server/tests/test_replay_subagent.py` — **new** tests.

**Frontend (TypeScript/React):**
- `web/src/session/events.ts` — `agentId` on content events; `EvSubAgentUpdate`. (modify)
- `web/src/session/reducer.ts` — `Turn.agentId`; `subAgents` state; `subAgentUpdate` handling. (modify)
- `web/src/session/selectors.ts` — `selectSubAgentTree`, `selectTurnsForAgent`. (modify)
- `web/src/session/useSessionStream.ts` — **new** hook: WS attach + reducer (extracted from `SessionView`).
- `web/src/api/types.ts` — `SubAgentSummary`; `Session.subAgents`. (modify)
- `web/src/components/session/SessionView.tsx` — use the hook; render only the main bucket. (modify)
- `web/src/components/session/SubSessionView.tsx` — **new** read-only filtered view + breadcrumb.
- `web/src/components/SessionList.tsx` / `SessionCard.tsx` — recursive nested sub-agent rows. (modify)
- `web/src/App.tsx` — `subSession` view state + routing. (modify)

---

## Task 1: Capture a real sub-agent transcript fixture (verification gate)

**Files:**
- Create: `server/tests/fixtures/transcript_subagent.jsonl`
- Modify (append findings): `docs/superpowers/specs/2026-06-22-subagent-session-drilldown-design.md`

**Interfaces:**
- Produces: a real JSONL fixture containing a main turn that spawns a `Task` sub-agent (ideally one with a nested Task too), plus the documented field that links a sidechain entry to its spawning Task `tool_use` id. Tasks 4 and the `subagents.py` helper depend on the confirmed linkage.

This task is empirical (no unit test) — its deliverable is the fixture + a documented "Observed sidechain linkage" note.

- [ ] **Step 1: Run a session that uses a sub-agent**

Use the running app (or `claude` directly) to run a prompt that triggers the `Task` tool, e.g. "Use the Explore subagent to find where X is defined." Let it complete. Prefer a run where the sub-agent itself spawns a Task (for nesting); if not feasible, a single level is acceptable and nesting is covered by a hand-authored fixture in Task 4.

- [ ] **Step 2: Locate the transcript JSONL**

```bash
ls -t ~/.claude*/projects/*/*.jsonl 2>/dev/null | head
# or, the config dir for the account used:
ls -t "$CLAUDE_CONFIG_DIR"/projects/*/*.jsonl | head
```

Open the newest file. Identify: (a) the main `assistant` entry with a `tool_use` block `"name":"Task"` and its `id`; (b) the entries with `"isSidechain":true`; (c) how each sidechain entry references the Task — inspect `parentUuid`, and whether any field carries the Task `tool_use` id directly.

- [ ] **Step 3: Build the trimmed fixture**

Copy the relevant contiguous lines (the user turn, the main assistant Task turn, all sidechain entries, and the `tool_result` user entry for the Task) into `server/tests/fixtures/transcript_subagent.jsonl`. Redact any real paths/secrets but keep the structural fields (`type`, `uuid`, `parentUuid`, `isSidechain`, `message.content`, tool ids). Keep it small (≈10–20 lines).

- [ ] **Step 4: Document the observed linkage**

Append a short subsection to the design doc titled `## Observed sidechain linkage (verified <date>)` stating exactly how a sidechain entry maps to its Task id. Record which of these holds:
  - (A) sidechain entries carry the Task `tool_use` id in a field (name it), OR
  - (B) no direct field — the sidechain root's `parentUuid` points to the main assistant entry containing the Task, and grouping is by distinct sidechain `parentUuid` roots associated to Task `tool_use`s in document order.

Task 4 implements whichever is true; (B) is the fallback the helper is written against.

- [ ] **Step 5: Commit**

```bash
git add server/tests/fixtures/transcript_subagent.jsonl docs/superpowers/specs/2026-06-22-subagent-session-drilldown-design.md
git commit -m "test(fixtures): real sub-agent transcript + observed sidechain linkage"
```

---

## Task 2: Add `agent_id` to events and a `SubAgentUpdate` event

**Files:**
- Modify: `server/sessions/events.py`
- Test: `server/tests/test_events_agent_id.py` (new)

**Interfaces:**
- Produces:
  - Each of `TurnStart, TurnEnd, TextDelta, ThinkingDelta, ToolCall, ToolResult, UserMessage` gains a trailing `agent_id: str | None = None` field; `to_wire()` adds `"agentId": self.agent_id`.
  - `SubAgentUpdate(agent_id: str, parent_agent_id: str | None, agent_type: str, description: str, status: str)` with `to_wire()` → `{"type":"subAgentUpdate","agentId","parentAgentId","agentType","description","status"}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_events_agent_id.py
from server.sessions import events as ev

def test_content_events_carry_agent_id_in_wire():
    assert ev.TextDelta("t1", "hi", "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"
    assert ev.ToolCall("t1", "tc1", "Read", ev.tool_kind("Read"), {}, "u1").to_wire()["agentId"] is None
    assert ev.TurnStart("t1", "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"
    assert ev.ToolResult("tc1", True, {}, "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"

def test_subagent_update_wire():
    e = ev.SubAgentUpdate(agent_id="toolu_A", parent_agent_id=None,
                          agent_type="Explore", description="find X", status="running")
    assert e.to_wire() == {"type": "subAgentUpdate", "agentId": "toolu_A",
                           "parentAgentId": None, "agentType": "Explore",
                           "description": "find X", "status": "running"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/test_events_agent_id.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'agent_id'` and `AttributeError: module ... has no attribute 'SubAgentUpdate'`.

- [ ] **Step 3: Implement**

In `server/sessions/events.py`, add `agent_id: str | None = None` as the **last** field of each listed dataclass and add it to its `to_wire()`. Example for `TextDelta` and `ToolCall`:

```python
@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None; agent_id: str | None = None
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "agentId": self.agent_id}

@dataclass
class ToolCall:
    turn_id: str; tool_id: str; name: str; kind: ToolKind; input: dict
    src_uuid: str | None = None; agent_id: str | None = None
    def to_wire(self): return {"type": "toolCall", "turnId": self.turn_id, "toolId": self.tool_id,
                               "name": self.name, "toolKind": self.kind.value, "input": self.input,
                               "srcUuid": self.src_uuid, "agentId": self.agent_id}
```

Apply the same pattern to `TurnStart, TurnEnd, ThinkingDelta, ToolResult, UserMessage`. Then add:

```python
@dataclass
class SubAgentUpdate:
    agent_id: str
    parent_agent_id: str | None
    agent_type: str
    description: str
    status: str  # "running" | "done" | "failed"
    def to_wire(self):
        return {"type": "subAgentUpdate", "agentId": self.agent_id,
                "parentAgentId": self.parent_agent_id, "agentType": self.agent_type,
                "description": self.description, "status": self.status}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/test_events_agent_id.py -q`
Expected: PASS. Also run the existing event/replay tests to confirm no regressions: `... -m pytest server/tests/ -q -k "event or replay or translator"`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events_agent_id.py
git commit -m "feat(events): agent_id on content events + SubAgentUpdate"
```

---

## Task 3: `Translator` — per-agent-context turns, agent_id tagging, SubAgentUpdate

**Files:**
- Modify: `server/sessions/translator.py`
- Test: `server/tests/test_translator_subagent.py` (new)

**Interfaces:**
- Consumes: SDK message objects exposing `parent_tool_use_id` (confirmed present on `StreamEvent`, `AssistantMessage`, `UserMessage`); `ResultMessage` has `uuid` but no `parent_tool_use_id` in older SDKs — read it via `getattr(msg, "parent_tool_use_id", None)`.
- Produces: `Translator().feed(msg)` returns events tagged with `agent_id` = the message's `parent_tool_use_id`; emits `SubAgentUpdate(status="running")` when a main/parent agent issues a `Task` ToolCall, and `SubAgentUpdate(status="done"/"failed")` when that Task's `tool_result` arrives.

**Design notes for the implementer (read before coding):**
- Today the translator keeps ONE turn context (`_started`, `_turn_id`, `_seen_tools`). Sub-agent messages interleave with the main agent, so these become **per-context dicts keyed by `agent_id`** (`None` = main).
- A sub-agent's own `ResultMessage` must end only THAT context — route `ResultMessage` by `parent_tool_use_id`. Only the main context's `ResultMessage` (`parent_tool_use_id is None`) resets the main turn.
- The `Task` tool-use appears as a `ToolUseBlock` in the *issuing* agent's `AssistantMessage` (its `agent_id` = the issuer's `parent_tool_use_id`). The sub-agent it creates has `agent_id == that ToolUseBlock.id`. The Task's `tool_result` arrives as a `ToolResultBlock` in a `UserMessage` whose `parent_tool_use_id` is the issuer's context (so its `tool_use_id` equals the sub-agent's `agent_id`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_translator_subagent.py
import dataclasses as dc
from claude_agent_sdk import (SystemMessage, AssistantMessage, UserMessage, ResultMessage,
                              ToolUseBlock, ToolResultBlock, TextBlock)
from server.sessions.translator import Translator
from server.sessions import events as ev

def _types(out): return [type(e).__name__ for e in out]

def test_task_call_emits_subagent_update_and_tags_main_null():
    tr = Translator()
    # Main agent issues a Task tool call (parent_tool_use_id is None).
    am = AssistantMessage(content=[ToolUseBlock(id="toolu_A", name="Task",
                                                input={"subagent_type": "Explore", "description": "find X"})],
                          model="m", parent_tool_use_id=None, uuid="u-main")
    out = tr.feed(am)
    sub = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    calls = [e for e in out if isinstance(e, ev.ToolCall)]
    assert calls and calls[0].agent_id is None and calls[0].tool_id == "toolu_A"
    assert sub and sub[0].agent_id == "toolu_A" and sub[0].parent_agent_id is None
    assert sub[0].agent_type == "Explore" and sub[0].status == "running"

def test_subagent_text_tagged_with_agent_id():
    tr = Translator()
    am = AssistantMessage(content=[TextBlock(text="working")], model="m",
                          parent_tool_use_id="toolu_A", uuid="u-sub")
    out = tr.feed(am)
    # Sub-agent tool/text events must carry agent_id = "toolu_A"
    starts = [e for e in out if isinstance(e, ev.TurnStart)]
    assert starts and starts[0].agent_id == "toolu_A"

def test_task_result_emits_done():
    tr = Translator()
    tr.feed(AssistantMessage(content=[ToolUseBlock(id="toolu_A", name="Task",
                                                   input={"subagent_type": "Explore", "description": "d"})],
                             model="m", parent_tool_use_id=None, uuid="u-main"))
    out = tr.feed(UserMessage(content=[ToolResultBlock(tool_use_id="toolu_A", content="ok", is_error=False)],
                              parent_tool_use_id=None, uuid="u-res"))
    sub = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    assert sub and sub[0].agent_id == "toolu_A" and sub[0].status == "done"
```

> If a block/message constructor signature differs in the installed SDK, adjust the test's kwargs to match — the asserted *behavior* is the contract. Verify signatures with `python -c "import claude_agent_sdk, inspect; print([f for f in AssistantMessage.__dataclass_fields__])"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/test_translator_subagent.py -q`
Expected: FAIL — events carry `agent_id is None` for sub-agent messages and no `SubAgentUpdate` is emitted.

- [ ] **Step 3: Implement the per-context translator**

Rewrite `server/sessions/translator.py` so context state is per `agent_id`:

```python
from __future__ import annotations
import dataclasses as _dc
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock)
from . import events as ev


def _usage_dict(u):
    if u is None: return None
    if isinstance(u, dict): return u
    if _dc.is_dataclass(u): return _dc.asdict(u)
    if hasattr(u, "__dict__"): return dict(vars(u))
    return None


class _Ctx:
    __slots__ = ("started", "turn_id", "seen_tools")
    def __init__(self):
        self.started = False
        self.turn_id = ""
        self.seen_tools = set()


class Translator:
    def __init__(self):
        self.session_id: str | None = None
        self._ctx: dict[str | None, _Ctx] = {}
        self._meta_emitted = False
        # Known Task tool-uses: agent_id -> {"type":..., "description":..., "parent": parent_agent_id}
        self._tasks: dict[str, dict] = {}

    def _c(self, agent_id) -> _Ctx:
        c = self._ctx.get(agent_id)
        if c is None:
            c = self._ctx[agent_id] = _Ctx()
        return c

    def _ensure_turn(self, agent_id, uuid) -> list:
        c = self._c(agent_id)
        if not c.started:
            c.turn_id = c.turn_id or uuid or "turn"
            c.started = True
            return [ev.TurnStart(c.turn_id, uuid, agent_id=agent_id)]
        return []

    @staticmethod
    def _agent(msg):
        return getattr(msg, "parent_tool_use_id", None)

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model and not self._meta_emitted:
                    self._meta_emitted = True
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
            return []

        if isinstance(msg, StreamEvent):
            agent_id = self._agent(msg)
            e = msg.event or {}
            t = e.get("type")
            if t == "message_start":
                return self._ensure_turn(agent_id, msg.uuid)
            if t == "content_block_delta":
                out = self._ensure_turn(agent_id, msg.uuid)
                c = self._c(agent_id)
                delta = e.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out.append(ev.TextDelta(c.turn_id, delta.get("text", ""), msg.uuid, agent_id=agent_id))
                elif dt == "thinking_delta":
                    out.append(ev.ThinkingDelta(c.turn_id, delta.get("thinking", ""), msg.uuid, agent_id=agent_id))
                return out
            return []

        if isinstance(msg, AssistantMessage):
            agent_id = self._agent(msg)
            uuid = getattr(msg, "uuid", None)
            out = self._ensure_turn(agent_id, uuid)
            c = self._c(agent_id)
            for b in msg.content:
                if isinstance(b, ToolUseBlock) and b.id not in c.seen_tools:
                    c.seen_tools.add(b.id)
                    if b.name == "AskUserQuestion":
                        continue
                    out.append(ev.ToolCall(c.turn_id, b.id, b.name, ev.tool_kind(b.name),
                                           b.input, uuid, agent_id=agent_id))
                    if b.name == "Task":
                        inp = b.input if isinstance(b.input, dict) else {}
                        atype = inp.get("subagent_type") or "agent"
                        desc = inp.get("description") or ""
                        self._tasks[b.id] = {"type": atype, "description": desc, "parent": agent_id}
                        out.append(ev.SubAgentUpdate(agent_id=b.id, parent_agent_id=agent_id,
                                                     agent_type=str(atype), description=str(desc),
                                                     status="running"))
            return out

        if isinstance(msg, UserMessage):
            agent_id = self._agent(msg)
            uuid = getattr(msg, "uuid", None)
            out = []
            for rb in msg.content:
                if isinstance(rb, ToolResultBlock):
                    ok = not bool(rb.is_error)
                    out.append(ev.ToolResult(rb.tool_use_id, ok, {"content": rb.content},
                                             uuid, agent_id=agent_id))
                    task = self._tasks.get(rb.tool_use_id)
                    if task is not None:
                        out.append(ev.SubAgentUpdate(agent_id=rb.tool_use_id, parent_agent_id=task["parent"],
                                                     agent_type=str(task["type"]), description=str(task["description"]),
                                                     status="done" if ok else "failed"))
            return out

        if isinstance(msg, ResultMessage):
            agent_id = self._agent(msg)
            self.session_id = msg.session_id or self.session_id
            c = self._c(agent_id)
            tid = c.turn_id or "turn"
            # Reset only this context.
            self._ctx[agent_id] = _Ctx()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                               agent_id=agent_id)]

        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/test_translator_subagent.py server/tests/ -q -k "translator"`
Expected: PASS (new + existing translator tests). The existing single-agent tests still pass because `agent_id` defaults to `None`.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/translator.py server/tests/test_translator_subagent.py
git commit -m "feat(translator): per-agent-context turns, agent_id tagging, SubAgentUpdate"
```

---

## Task 4: Sub-agent tree helper + replay tagging

**Files:**
- Create: `server/sessions/subagents.py`
- Modify: `server/sessions/replay.py`
- Test: `server/tests/test_subagents.py` (new), `server/tests/test_replay_subagent.py` (new)

**Interfaces:**
- Produces:
  - `subagents.summarize(events: list) -> list[dict]` — given the wire-or-object event list, return a flat list of `{"agentId","parentAgentId","agentType","description","status"}` (latest status per agent wins). Used by the orchestrator + API.
  - `replay._parse` tags every emitted event with `agent_id` and emits `SubAgentUpdate` events for each Task found.

**Replay linkage (verified 2026-06-22 — defensive, user-approved):** no existing ccswitch transcript contains a sub-agent run, so the parser tolerates BOTH shapes per entry, in priority order:
  1. non-null `parent_tool_use_id` (top-level or inside `message`) → use it as `agent_id` (mirrors the live path; simplest);
  2. else truthy `isSidechain` → fallback (B): group by distinct `parentUuid` roots, associate each root to a main-agent `Task` tool-use in document order;
  3. else → `agent_id = None` (main agent).
The fixture from Task 1 exercises shape (2). A real sub-agent transcript is captured at final manual smoke to confirm before merge.

- [ ] **Step 1: Write the failing helper test**

```python
# server/tests/test_subagents.py
from server.sessions import events as ev
from server.sessions.subagents import summarize

def test_summarize_latest_status_wins():
    evs = [
        ev.SubAgentUpdate("toolu_A", None, "Explore", "find X", "running"),
        ev.SubAgentUpdate("toolu_B", "toolu_A", "agent", "nested", "running"),
        ev.SubAgentUpdate("toolu_A", None, "Explore", "find X", "done"),
    ]
    out = summarize(evs)
    by_id = {s["agentId"]: s for s in out}
    assert by_id["toolu_A"]["status"] == "done"
    assert by_id["toolu_B"]["parentAgentId"] == "toolu_A"
    assert len(out) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd <worktree> && <main-checkout>/.venv/bin/python -m pytest server/tests/test_subagents.py -q`
Expected: FAIL — `ModuleNotFoundError: server.sessions.subagents`.

- [ ] **Step 3: Implement the helper**

```python
# server/sessions/subagents.py
from __future__ import annotations
from . import events as ev


def summarize(events: list) -> list[dict]:
    """Reduce a list of events to the latest sub-agent summary per agentId.

    Accepts either SubAgentUpdate objects or their to_wire() dicts; ignores
    everything else. Preserves first-seen order, last-status-wins.
    """
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for e in events:
        d = e.to_wire() if hasattr(e, "to_wire") else e
        if not isinstance(d, dict) or d.get("type") != "subAgentUpdate":
            continue
        aid = d["agentId"]
        if aid not in by_id:
            order.append(aid)
        by_id[aid] = {"agentId": aid, "parentAgentId": d.get("parentAgentId"),
                      "agentType": d.get("agentType", "agent"),
                      "description": d.get("description", ""), "status": d.get("status", "running")}
    return [by_id[a] for a in order]
```

- [ ] **Step 4: Run helper test (PASS), then write the replay test**

Run: `... -m pytest server/tests/test_subagents.py -q` → PASS.

```python
# server/tests/test_replay_subagent.py
import asyncio, os
from server.sessions.replay import replay_events
from server.sessions import events as ev

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_subagent.jsonl")

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_replay_tags_sidechain_with_agent_id_and_excludes_from_main():
    out = _run(replay_events(FIX))
    # There is at least one SubAgentUpdate and at least one event tagged with an agent_id.
    subs = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    assert subs, "expected a SubAgentUpdate from the Task in the fixture"
    sub_id = subs[0].agent_id
    tagged = [e for e in out if getattr(e, "agent_id", None) == sub_id]
    assert tagged, "sidechain events must be tagged with the sub-agent's agent_id"
    # The main thread (agent_id is None) must NOT contain the sub-agent's text.
    main_text = "".join(getattr(e, "text", "") for e in out
                        if isinstance(e, ev.TextDelta) and getattr(e, "agent_id", None) is None)
    sub_text = "".join(getattr(e, "text", "") for e in tagged if isinstance(e, ev.TextDelta))
    if sub_text:
        assert sub_text not in main_text
```

- [ ] **Step 5: Run replay test to verify it fails**

Run: `... -m pytest server/tests/test_replay_subagent.py -q`
Expected: FAIL — current `replay._parse` emits no `SubAgentUpdate` and tags nothing.

- [ ] **Step 6: Implement replay tagging**

In `server/sessions/replay.py`, change `_parse` to (1) pre-scan for sidechain grouping, (2) tag each event with `agent_id`, (3) emit `SubAgentUpdate`. Add this grouping before the main loop and thread `agent_id` into every `ev.*` constructor:

```python
def _entry_ptui(o: dict):
    """Non-null parent_tool_use_id from top-level or message, else None."""
    v = o.get("parent_tool_use_id")
    if v:
        return v
    m = o.get("message")
    if isinstance(m, dict) and m.get("parent_tool_use_id"):
        return m.get("parent_tool_use_id")
    return None


def _agent_ids(lines: list) -> dict:
    """Map each entry uuid -> owning agent_id (None = main), defensively.

    Priority per entry: (1) non-null parent_tool_use_id wins; (2) else
    isSidechain entries are grouped by parentUuid roots and associated to
    main-agent Task tool_uses in document order; (3) else None.
    """
    by_uuid = {o.get("uuid"): o for o in lines if o.get("uuid")}
    # Task tool_use ids in document order (main agent only).
    task_ids = []
    for o in lines:
        if o.get("isSidechain"):
            continue
        for b in ((o.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Task":
                task_ids.append(b.get("id"))

    def root_of(o):
        seen = set()
        cur = o
        while cur is not None and cur.get("isSidechain") and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            parent = by_uuid.get(cur.get("parentUuid"))
            if parent is None or not parent.get("isSidechain"):
                return cur.get("uuid")
            cur = parent
        return o.get("uuid")

    roots = []
    for o in lines:
        if o.get("isSidechain"):
            r = root_of(o)
            if r not in roots:
                roots.append(r)
    root_to_agent = {r: (task_ids[i] if i < len(task_ids) else f"sidechain:{r}")
                     for i, r in enumerate(roots)}
    out = {}
    for o in lines:
        ptui = _entry_ptui(o)
        if ptui:                                   # (1) explicit link wins
            out[o.get("uuid")] = ptui
        elif o.get("isSidechain"):                 # (2) sidechain grouping
            out[o.get("uuid")] = root_to_agent.get(root_of(o))
        else:                                      # (3) main agent
            out[o.get("uuid")] = None
    return out
```

Then in `_parse`, after building `lines`, compute `agents = _agent_ids(lines)`, set `aid = agents.get(uuid)` per entry, pass `agent_id=aid` into every emitted event, and emit `SubAgentUpdate`s. For each main-agent `Task` tool_use emit `running`; when its `tool_result` is seen emit `done`/`failed` (track Task input by id, mirroring the translator). Concretely, inside the `assistant` branch when `b.get("name") == "Task"`:

```python
                elif t == "tool_use":
                    out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                                           ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid, agent_id=aid))
                    if b.get("name") == "Task" and aid is None:
                        inp = b.get("input") or {}
                        tasks[b.get("id", "")] = {"type": inp.get("subagent_type") or "agent",
                                                  "description": inp.get("description") or ""}
                        out.append(ev.SubAgentUpdate(b.get("id", ""), None, str(inp.get("subagent_type") or "agent"),
                                                     str(inp.get("description") or ""), "running"))
```

And in the `user` branch where `tool_result`s are handled, after appending the `ToolResult` (now with `agent_id=aid`), emit the terminal `SubAgentUpdate` when `r["tool_use_id"]` is in `tasks`:

```python
            for r in results:
                ok = not r.get("is_error", False)
                out.append(ev.ToolResult(r["tool_use_id"], ok, {"content": r.get("content")}, uuid, agent_id=aid))
                task = tasks.get(r["tool_use_id"])
                if task is not None:
                    out.append(ev.SubAgentUpdate(r["tool_use_id"], None, str(task["type"]),
                                                 str(task["description"]), "done" if ok else "failed"))
```

Initialize `tasks = {}` at the top of `_parse`. Pass `agent_id=aid` into the `UserMessage`, `TurnStart`, `TextDelta`, `ThinkingDelta`, and `TurnEnd` constructors too.

- [ ] **Step 7: Run all replay tests to verify they pass**

Run: `... -m pytest server/tests/test_replay_subagent.py server/tests/ -q -k "replay or subagent"`
Expected: PASS. The pre-existing `transcript.jsonl` (no sidechain) test still passes — all `agent_id` values are `None`.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/subagents.py server/sessions/replay.py server/tests/test_subagents.py server/tests/test_replay_subagent.py
git commit -m "feat(replay): tag sidechain events with agent_id + emit SubAgentUpdate"
```

---

## Task 5: Persist sub-agent summaries + orchestrator accessor + API field

**Files:**
- Modify: `server/db/models.py`, `server/sessions/orchestrator.py`, `server/api/sessions.py`
- Test: `server/tests/test_sessions_subagents_api.py` (new)

**Interfaces:**
- Consumes: `subagents.summarize(events)`; `orchestrator.transcript_events(session_id)` (existing).
- Produces:
  - `Session.sub_agents` — nullable `Text` column holding a JSON-encoded summary list (mirrors how `tool_allowlist` is stored — confirm its column type and copy the pattern).
  - `orchestrator.sub_agents(session_id) -> list[dict]` — active: `summarize(live transcript_events)`; archived: parse the persisted column (fallback to `summarize(await transcript_events(...))`).
  - `_record_to_dict(record)` includes `"subAgents": <list>` (empty list when none).

- [ ] **Step 1: Write the failing API test**

```python
# server/tests/test_sessions_subagents_api.py
import json
from server.api.sessions import _record_to_dict

class _Rec:
    id = "s1"; name = "n"; account = "a"; status = "active"; repo = None
    created_at = updated_at = None; pid = 1; cwd = "/tmp"; title = None
    permission_mode = "interactive"; claude_session_id = None
    name_is_auto = False; tool_allowlist = None
    sub_agents = json.dumps([{"agentId": "toolu_A", "parentAgentId": None,
                              "agentType": "Explore", "description": "d", "status": "done"}])

def test_record_to_dict_includes_subagents():
    d = _record_to_dict(_Rec())
    assert d["subAgents"][0]["agentId"] == "toolu_A"

def test_record_to_dict_subagents_empty_when_null():
    r = _Rec(); r.sub_agents = None
    assert _record_to_dict(r)["subAgents"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd <worktree> && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_subagents_api.py -q`
Expected: FAIL — `KeyError: 'subAgents'`.

- [ ] **Step 3: Add the column**

In `server/db/models.py`, on `Session`, add (matching the surrounding column style and the nullable rule):

```python
    sub_agents = Column(Text, nullable=True)  # JSON-encoded list[SubAgentSummary]; backfilled nullable
```

- [ ] **Step 4: Serialize it in the API**

In `server/api/sessions.py` `_record_to_dict`, add:

```python
    import json as _json
    raw = getattr(record, "sub_agents", None)
    try:
        sub_agents = _json.loads(raw) if raw else []
    except (ValueError, TypeError):
        sub_agents = []
    # ... in the returned dict:
        "subAgents": sub_agents,
```

- [ ] **Step 5: Add the orchestrator accessor + persistence**

In `server/sessions/orchestrator.py` add:

```python
    async def sub_agents(self, session_id: str) -> list:
        from .subagents import summarize
        events = await self.transcript_events(session_id)
        return summarize(events)
```

Persist on the same path that already writes session metadata to the DB (e.g. where `claude_session_id`/title are persisted): after a turn ends or at archive time, write `record.sub_agents = json.dumps(await self.sub_agents(session_id))`. Reuse the existing DB-write helper rather than opening a new session. (Find it via `grep -n "claude_session_id =" server/sessions/orchestrator.py` — write `sub_agents` alongside.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd <worktree> && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_subagents_api.py server/tests/ -q -k "session or db or orchestrator"`
Expected: PASS. Confirm `init_db` backfill works (existing DB tests green — the new column is nullable).

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/api/sessions.py server/sessions/orchestrator.py server/tests/test_sessions_subagents_api.py
git commit -m "feat(api): persist + expose per-session sub-agent summaries"
```

---

## Task 6: Frontend events + reducer (agentId on turns, subAgents map)

**Files:**
- Modify: `web/src/session/events.ts`, `web/src/session/reducer.ts`
- Test: `web/src/session/__tests__/reducer.subagents.test.ts` (new)

**Interfaces:**
- Produces:
  - `events.ts`: optional `agentId?: string | null` on `EvUser, EvTurnStart, EvTurnEnd, EvTextDelta, EvThinkingDelta, EvToolCall, EvToolResult`; new `EvSubAgentUpdate { type:'subAgentUpdate'; agentId; parentAgentId; agentType; description; status }`; both added to `SessionEvent` and `SESSION_EVENT_TYPES`.
  - `reducer.ts`: `Turn` gains `agentId?: string | null`; `TranscriptState` gains `subAgents: Record<string, { agentId; parentAgentId; agentType; description; status }>`; the reducer sets `agentId` on turn creation and folds `subAgentUpdate`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/reducer.subagents.test.ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'

describe('reducer sub-agent attribution', () => {
  it('tags turns with agentId from the event', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'm1', agentId: null })
    s = transcriptReducer(s, { type: 'turnStart', turnId: 's1', agentId: 'toolu_A' })
    expect(s.turns.find((t) => t.id === 'm1')?.agentId ?? null).toBe(null)
    expect(s.turns.find((t) => t.id === 's1')?.agentId).toBe('toolu_A')
  })

  it('folds subAgentUpdate into the subAgents map (latest status wins)', () => {
    let s = initialTranscript
    s = transcriptReducer(s, { type: 'subAgentUpdate', agentId: 'toolu_A', parentAgentId: null,
                               agentType: 'Explore', description: 'd', status: 'running' })
    s = transcriptReducer(s, { type: 'subAgentUpdate', agentId: 'toolu_A', parentAgentId: null,
                               agentType: 'Explore', description: 'd', status: 'done' })
    expect(s.subAgents['toolu_A'].status).toBe('done')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/session/__tests__/reducer.subagents.test.ts`
Expected: FAIL — `subAgentUpdate` unhandled; `agentId` not on turns; `subAgents` undefined.

- [ ] **Step 3: Implement events.ts**

Add `agentId?: string | null` to the listed `Ev*` interfaces. Add:

```ts
export interface EvSubAgentUpdate { type: 'subAgentUpdate'; agentId: string; parentAgentId: string | null; agentType: string; description: string; status: 'running' | 'done' | 'failed' }
```

Add `EvSubAgentUpdate` to the `SessionEvent` union and `'subAgentUpdate'` to `SESSION_EVENT_TYPES`.

- [ ] **Step 4: Implement reducer.ts**

- Add `agentId?: string | null` to `Turn`.
- Add to `TranscriptState`: `subAgents: Record<string, { agentId: string; parentAgentId: string | null; agentType: string; description: string; status: 'running' | 'done' | 'failed' }>` and initialize `subAgents: {}` in `initialTranscript`.
- Set `agentId` when creating turns. Change `ensureAssistantTurn` to accept an `agentId`:

```ts
function ensureAssistantTurn(turns: Turn[], turnId: string, agentId?: string | null): [Turn[], number] {
  const idx = findTurnIdx(turns, turnId)
  if (idx !== -1) return [turns, idx]
  return [[...turns, { id: turnId, role: 'assistant', blocks: [], agentId: agentId ?? null }], turns.length]
}
```

Pass `ev.agentId` from the `textDelta`, `thinkingDelta`, `toolCall` cases (they all call `ensureAssistantTurn`). In `turnStart`'s explicit append, set `agentId: ev.agentId ?? null`. In the `user` case, set `agentId: ev.agentId ?? null` on the created turn. The `permissionRequest`/`question` cases do **not** carry `agentId` (those events were not extended) — call `ensureAssistantTurn(state.turns, turnId)` without an agent id; if their turn doesn't exist yet it defaults to the main bucket, which is correct (interactive prompts surface to the main thread). Add the new case:

```ts
    case 'subAgentUpdate': {
      const { agentId, parentAgentId, agentType, description, status } = ev
      return { ...state, subAgents: { ...state.subAgents,
        [agentId]: { agentId, parentAgentId, agentType, description, status } } }
    }
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd web && npx vitest run src/session/__tests__/reducer.subagents.test.ts && npx vitest run src/session && npx tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/__tests__/reducer.subagents.test.ts
git commit -m "feat(web): agentId on turns + subAgents map in reducer"
```

---

## Task 7: Selectors — sub-agent tree + per-agent transcript

**Files:**
- Modify: `web/src/session/selectors.ts`
- Test: `web/src/session/__tests__/selectors.test.ts` (new)

**Interfaces:**
- Consumes: `TranscriptState.subAgents`, `Turn.agentId`.
- Produces:
  - `selectTurnsForAgent(turns: Turn[], agentId: string | null): Turn[]` — turns whose `agentId` matches (`null` = main thread).
  - `interface SubAgentNode extends SubAgent { parentAgentId: string | null; children: SubAgentNode[] }`
  - `selectSubAgentTree(subAgents: TranscriptState['subAgents']): SubAgentNode[]` — roots (`parentAgentId == null`) with nested `children`, arbitrary depth.
  - Keep the existing `SubAgent`/`SubAgentStatus` exports (the status strip still imports `selectSubAgents`). Re-implement `selectSubAgents` in terms of the new map so it keeps working: flat list from `subAgents` values. Update `SessionView`'s call accordingly (Task 9).

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/selectors.test.ts
import { describe, it, expect } from 'vitest'
import { selectTurnsForAgent, selectSubAgentTree } from '../selectors'
import type { Turn } from '../reducer'

const turns: Turn[] = [
  { id: 'm1', role: 'assistant', blocks: [], agentId: null },
  { id: 's1', role: 'assistant', blocks: [], agentId: 'toolu_A' },
]

describe('selectors', () => {
  it('filters turns by agentId', () => {
    expect(selectTurnsForAgent(turns, null).map((t) => t.id)).toEqual(['m1'])
    expect(selectTurnsForAgent(turns, 'toolu_A').map((t) => t.id)).toEqual(['s1'])
  })

  it('builds a nested tree to arbitrary depth', () => {
    const map = {
      toolu_A: { agentId: 'toolu_A', parentAgentId: null, agentType: 'Explore', description: 'd', status: 'done' as const },
      toolu_B: { agentId: 'toolu_B', parentAgentId: 'toolu_A', agentType: 'agent', description: 'n', status: 'running' as const },
    }
    const tree = selectSubAgentTree(map)
    expect(tree).toHaveLength(1)
    expect(tree[0].agentId).toBe('toolu_A')
    expect(tree[0].children[0].agentId).toBe('toolu_B')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/session/__tests__/selectors.test.ts`
Expected: FAIL — `selectTurnsForAgent`/`selectSubAgentTree` not exported.

- [ ] **Step 3: Implement**

Append to `web/src/session/selectors.ts`:

```ts
import type { TranscriptState } from './reducer'

export function selectTurnsForAgent(turns: Turn[], agentId: string | null): Turn[] {
  return turns.filter((t) => (t.agentId ?? null) === agentId)
}

export interface SubAgentNode extends SubAgent {
  parentAgentId: string | null
  children: SubAgentNode[]
}

export function selectSubAgentTree(subAgents: TranscriptState['subAgents']): SubAgentNode[] {
  const nodes = new Map<string, SubAgentNode>()
  for (const s of Object.values(subAgents)) {
    nodes.set(s.agentId, { toolId: s.agentId, type: s.agentType, description: s.description,
      status: s.status, parentAgentId: s.parentAgentId, children: [] })
  }
  const roots: SubAgentNode[] = []
  for (const n of nodes.values()) {
    const parent = n.parentAgentId ? nodes.get(n.parentAgentId) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  return roots
}
```

Replace the body of `selectSubAgents` so it derives from the map shape too — change its signature to `selectSubAgents(subAgents: TranscriptState['subAgents']): SubAgent[]` returning `Object.values(subAgents).map((s) => ({ toolId: s.agentId, type: s.agentType, description: s.description, status: s.status }))`. (Task 9 updates the one caller.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd web && npx vitest run src/session/__tests__/selectors.test.ts && npx tsc --noEmit`
Expected: PASS. (tsc may flag the old `selectSubAgents(state.turns)` caller — fixed in Task 9; if running tsc now is red only on that line, proceed.)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/selectors.ts web/src/session/__tests__/selectors.test.ts
git commit -m "feat(web): sub-agent tree + per-agent transcript selectors"
```

---

## Task 8: Extract `useSessionStream` hook (shared live WS + reducer)

**Files:**
- Create: `web/src/session/useSessionStream.ts`
- Modify: `web/src/components/session/SessionView.tsx` (consume the hook)
- Test: covered indirectly by existing SessionView tests + tsc; add `web/src/session/__tests__/useSessionStream.test.tsx` if a test harness for hooks exists (optional — keep if green).

**Interfaces:**
- Produces: `useSessionStream(sessionId: string): { state: TranscriptState; dispatch: Dispatch<SessionEvent | LocalAction>; connectionState; on; off; attach; detach; ...senders }` — the WS attach/detach effect + reducer currently inlined in `SessionView`. `SessionView` and `SubSessionView` both consume it.

This is a refactor (no behavior change) so the test cycle is: extract → existing SessionView tests stay green → commit.

- [ ] **Step 1: Confirm the baseline is green**

Run: `cd web && npx vitest run src/components/session && npx tsc --noEmit`
Expected: PASS (record this as the regression baseline).

- [ ] **Step 2: Create the hook**

Move the `useReducer(transcriptReducer, initialTranscript)`, the attach/detach `useEffect` (lines ~75–98 of `SessionView`), and the socket accessors into `web/src/session/useSessionStream.ts`:

```ts
import { useEffect, useReducer } from 'react'
import { useSessionSocket } from '@/ws/client'
import type { WsRotatedMsg, WsErrorMsg } from '@/ws/client'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

export function useSessionStream(sessionId: string, onRotated?: (m: WsRotatedMsg) => void) {
  const sock = useSessionSocket()
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)

  useEffect(() => {
    const onEvent = (e: SessionEvent) => dispatch(e)
    const onErr = (m: WsErrorMsg) =>
      dispatch({ type: 'error', code: m.code, message: m.message, resetsAt: m.resetsAt ?? null })
    const onRot = (m: WsRotatedMsg) => { if (m.sessionId === sessionId) onRotated?.(m) }
    sock.on('sessionEvent', onEvent)
    sock.on('rotated', onRot)
    sock.on('error', onErr)
    sock.attach(sessionId)
    return () => {
      sock.off('sessionEvent', onEvent); sock.off('rotated', onRot); sock.off('error', onErr)
      sock.detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  return { state, dispatch, ...sock }
}
```

- [ ] **Step 3: Refactor `SessionView` to use it**

Replace the inlined `useReducer` + attach/detach effect in `SessionView` with `const { state, dispatch, connectionState, prompt, sendInterrupt, permissionResponse, answerQuestion, setPermissionMode, cancelQueued, detach } = useSessionStream(sessionId, (m) => setRotations((p) => [...p, { from: m.fromAccount, to: m.toAccount, reason: m.reason }]))`. Remove the now-dead `useSessionSocket()` destructure and the moved effect. Keep all other logic identical.

- [ ] **Step 4: Verify no regression**

Run: `cd web && npx vitest run src/components/session && npx tsc --noEmit && npx eslint src/session/useSessionStream.ts src/components/session/SessionView.tsx`
Expected: PASS — identical behavior, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionStream.ts web/src/components/session/SessionView.tsx
git commit -m "refactor(web): extract useSessionStream hook from SessionView"
```

---

## Task 9: Main thread shows only main-agent turns

**Files:**
- Modify: `web/src/components/session/SessionView.tsx`
- Test: `web/src/components/session/__tests__/SessionView.mainThread.test.tsx` (new)

**Interfaces:**
- Consumes: `selectTurnsForAgent`, `selectSubAgents` (new map signature), `state.subAgents`.
- Produces: `TranscriptView` receives only `selectTurnsForAgent(state.turns, null)`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/SessionView.mainThread.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { TranscriptView } from '../TranscriptView'
import { selectTurnsForAgent } from '@/session/selectors'
import type { Turn } from '@/session/reducer'

// Unit-level proof that the main thread excludes sub-agent turns.
describe('main thread filtering', () => {
  it('renders main-agent text but not sub-agent text', () => {
    const turns: Turn[] = [
      { id: 'm1', role: 'assistant', agentId: null, blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] },
      { id: 's1', role: 'assistant', agentId: 'toolu_A', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBAGENT_ONLY' }] },
    ]
    render(<TranscriptView turns={selectTurnsForAgent(turns, null)} permissionMode="interactive"
                           onDecision={() => {}} onAnswer={() => {}} />)
    expect(screen.getByText(/MAIN_ONLY/)).toBeInTheDocument()
    expect(screen.queryByText(/SUBAGENT_ONLY/)).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/SessionView.mainThread.test.tsx`
Expected: FAIL only if a prior wiring already passes everything — more importantly this locks the contract. If `TranscriptView` renders both before the SessionView change, this test passes already at the unit level; the SessionView wiring step (3) is what removes the leak in the real app. (Keep the test as a regression guard.)

- [ ] **Step 3: Wire SessionView**

In `SessionView.tsx`:
- Replace `const subAgents = selectSubAgents(state.turns)` with `const subAgents = selectSubAgents(state.subAgents)`.
- Add `import { selectSubAgents, selectTurnsForAgent } from '@/session/selectors'`.
- Change the `<TranscriptView turns={state.turns} ... />` to `turns={selectTurnsForAgent(state.turns, null)}`.
- Update the aria-live "last assistant text" effect to read from main-thread turns only (it already iterates `state.turns`; change to `selectTurnsForAgent(state.turns, null)` for the `lastAssistantTurn` search so screen readers don't announce sub-agent text).

- [ ] **Step 4: Verify**

Run: `cd web && npx vitest run src/components/session && npx tsc --noEmit && npx eslint src/components/session/SessionView.tsx`
Expected: PASS — main thread excludes sub-agent content; status strip sub-agent list still populates from the map.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/session/__tests__/SessionView.mainThread.test.tsx
git commit -m "feat(web): main thread renders only main-agent turns"
```

---

## Task 10: Session list — nested recursive sub-agent rows

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/components/SessionList.tsx`, `web/src/components/SessionCard.tsx`
- Create: `web/src/components/SubAgentRows.tsx`
- Test: `web/src/components/__tests__/SubAgentRows.test.tsx` (new)

**Interfaces:**
- Consumes: `Session.subAgents` (from the API, Task 5).
- Produces:
  - `api/types.ts`: `interface SubAgentSummary { agentId: string; parentAgentId: string | null; agentType: string; description: string; status: 'running'|'done'|'failed' }`; `Session.subAgents?: SubAgentSummary[]`.
  - `SubAgentRows({ sessionId, subAgents, depth, onOpenSubAgent })` — renders the tree (built via `selectSubAgentTree` adapted to the flat `SubAgentSummary[]`) as indented, recursive rows; each row calls `onOpenSubAgent(sessionId, agentId)`.
  - `SessionCard` gains optional `subAgents?: SubAgentSummary[]` and `onOpenSubAgent?: (sessionId: string, agentId: string) => void`; renders an expand chevron when non-empty and `<SubAgentRows>` when expanded.
  - `SessionList` threads `onOpenSubAgent` through to each card.

**Tree-from-flat helper:** add `subAgentTreeFromSummaries(list: SubAgentSummary[]): SubAgentNode[]` to `selectors.ts` (same algorithm as `selectSubAgentTree` but input is the flat array). Reuse in `SubAgentRows`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/SubAgentRows.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SubAgentRows } from '../SubAgentRows'
import type { SubAgentSummary } from '@/api/types'

const subs: SubAgentSummary[] = [
  { agentId: 'A', parentAgentId: null, agentType: 'Explore', description: 'find X', status: 'done' },
  { agentId: 'B', parentAgentId: 'A', agentType: 'agent', description: 'nested', status: 'running' },
]

describe('SubAgentRows', () => {
  it('renders nested rows and fires onOpenSubAgent', () => {
    const onOpen = vi.fn()
    render(<SubAgentRows sessionId="s1" subAgents={subs} depth={0} onOpenSubAgent={onOpen} />)
    expect(screen.getByText(/find X/)).toBeInTheDocument()
    expect(screen.getByText(/nested/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/find X/))
    expect(onOpen).toHaveBeenCalledWith('s1', 'A')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/components/__tests__/SubAgentRows.test.tsx`
Expected: FAIL — `SubAgentRows` does not exist.

- [ ] **Step 3: Implement types + helper + component**

In `api/types.ts` add `SubAgentSummary` and `subAgents?: SubAgentSummary[]` on `Session`.

In `selectors.ts` add:

```ts
import type { SubAgentSummary } from '@/api/types'
export function subAgentTreeFromSummaries(list: SubAgentSummary[]): SubAgentNode[] {
  const map: TranscriptState['subAgents'] = {}
  for (const s of list) map[s.agentId] = s
  return selectSubAgentTree(map)
}
```

Create `web/src/components/SubAgentRows.tsx`:

```tsx
import { subAgentTreeFromSummaries, type SubAgentNode } from '@/session/selectors'
import type { SubAgentSummary } from '@/api/types'
import { cn } from '@/lib/utils'

const DOT: Record<string, string> = {
  running: 'bg-amber-500 animate-pulse', done: 'bg-green-500', failed: 'bg-red-500',
}

function Row({ sessionId, node, depth, onOpenSubAgent }: {
  sessionId: string; node: SubAgentNode; depth: number
  onOpenSubAgent: (sessionId: string, agentId: string) => void
}) {
  return (
    <>
      <button
        type="button"
        onClick={() => onOpenSubAgent(sessionId, node.toolId)}
        style={{ paddingLeft: 12 + Math.min(depth, 6) * 16 }}
        className={cn('flex w-full items-center gap-2 py-1.5 text-left text-xs min-h-[36px]',
          'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 rounded')}
        aria-label={`Review sub-agent ${node.type}: ${node.description}`}
      >
        <span aria-hidden className={cn('w-2 h-2 rounded-full flex-shrink-0', DOT[node.status] ?? 'bg-gray-400')} />
        <span className="font-medium">{node.type}</span>
        <span className="truncate opacity-80">{node.description}</span>
      </button>
      {node.children.map((c) => (
        <Row key={c.toolId} sessionId={sessionId} node={c} depth={depth + 1} onOpenSubAgent={onOpenSubAgent} />
      ))}
    </>
  )
}

export function SubAgentRows({ sessionId, subAgents, depth, onOpenSubAgent }: {
  sessionId: string; subAgents: SubAgentSummary[]; depth: number
  onOpenSubAgent: (sessionId: string, agentId: string) => void
}) {
  const tree = subAgentTreeFromSummaries(subAgents)
  if (tree.length === 0) return null
  return (
    <div role="group" aria-label="Sub-agents" className="mt-1 border-t border-gray-100 dark:border-gray-700/60">
      {tree.map((n) => (
        <Row key={n.toolId} sessionId={sessionId} node={n} depth={depth} onOpenSubAgent={onOpenSubAgent} />
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Wire SessionCard + SessionList**

In `SessionCard.tsx`: add props `subAgents?: SubAgentSummary[]` and `onOpenSubAgent?: (sessionId: string, agentId: string) => void`. Add `const [expanded, setExpanded] = useState(false)`. When `subAgents?.length`, render a small chevron button (left of the title, `aria-expanded={expanded}`, `aria-label="Toggle sub-agents"`) toggling `expanded`. After the main `<button>`/menu row, when `expanded && onOpenSubAgent`, render `<SubAgentRows sessionId={session.id} subAgents={subAgents} depth={0} onOpenSubAgent={onOpenSubAgent} />`. (Place it inside the `<article>`, below the existing flex row — restructure the `<article>` to `flex-col` wrapping the existing row in a child `div`.)

In `SessionList.tsx`: add `onOpenSubAgent?: (sessionId, agentId) => void` to props and pass `subAgents={session.subAgents}` and `onOpenSubAgent={onOpenSubAgent}` to each `<SessionCard>`.

- [ ] **Step 5: Verify**

Run: `cd web && npx vitest run src/components && npx tsc --noEmit && npx eslint src/components/SubAgentRows.tsx src/components/SessionCard.tsx src/components/SessionList.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/types.ts web/src/session/selectors.ts web/src/components/SubAgentRows.tsx web/src/components/SessionCard.tsx web/src/components/SessionList.tsx web/src/components/__tests__/SubAgentRows.test.tsx
git commit -m "feat(web): nested sub-agent rows in the session list"
```

---

## Task 11: Sub-session view + routing + live status

**Files:**
- Create: `web/src/components/session/SubSessionView.tsx`
- Modify: `web/src/App.tsx`, `web/src/components/Dashboard.tsx`, `web/src/components/SessionList.tsx` (thread `onOpenSubAgent` from Dashboard)
- Test: `web/src/components/session/__tests__/SubSessionView.test.tsx` (new)

**Interfaces:**
- Consumes: `useSessionStream` (Task 8), `selectTurnsForAgent`, `state.subAgents`.
- Produces:
  - `SubSessionView({ sessionId, agentId, parentTitle, onBack })` — attaches via `useSessionStream`, renders `selectTurnsForAgent(state.turns, agentId)` read-only in `TranscriptView` with a breadcrumb header (`parentTitle › <agentType>`), and a running indicator when `state.subAgents[agentId]?.status === 'running'`.
  - `App` view union gains `{ type: 'subSession'; sessionId: string; agentId: string }`; `Dashboard` gains `onOpenSubAgent?: (sessionId, agentId) => void` passed to `SessionList`; `App.handleOpenSubAgent` sets the view.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/SubSessionView.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// Mock the stream hook to return a fixed transcript with a sub-agent turn.
vi.mock('@/session/useSessionStream', () => ({
  useSessionStream: () => ({
    state: {
      turns: [
        { id: 'm1', role: 'assistant', agentId: null, blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN' }] },
        { id: 's1', role: 'assistant', agentId: 'toolu_A', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] },
      ],
      subAgents: { toolu_A: { agentId: 'toolu_A', parentAgentId: null, agentType: 'Explore', description: 'd', status: 'running' } },
      replaying: false, status: { state: 'idle', count: 0 },
    },
    connectionState: 'open',
  }),
}))

import { SubSessionView } from '../SubSessionView'

describe('SubSessionView', () => {
  it('shows only the sub-agent transcript and a breadcrumb', () => {
    render(<SubSessionView sessionId="s1" agentId="toolu_A" parentTitle="Parent" onBack={vi.fn()} />)
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/^MAIN$/)).toBeNull()
    expect(screen.getByText(/Parent/)).toBeInTheDocument()
    expect(screen.getByText(/Explore/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/SubSessionView.test.tsx`
Expected: FAIL — `SubSessionView` does not exist.

- [ ] **Step 3: Implement SubSessionView**

```tsx
// web/src/components/session/SubSessionView.tsx
import { cn } from '@/lib/utils'
import { useSessionStream } from '@/session/useSessionStream'
import { selectTurnsForAgent } from '@/session/selectors'
import { TranscriptView } from './TranscriptView'

interface Props { sessionId: string; agentId: string; parentTitle: string; onBack: () => void }

export function SubSessionView({ sessionId, agentId, parentTitle, onBack }: Props) {
  const { state } = useSessionStream(sessionId)
  const turns = selectTurnsForAgent(state.turns, agentId)
  const meta = state.subAgents[agentId]
  const label = meta ? meta.agentType : 'sub-agent'

  return (
    <div className="flex flex-col h-screen bg-gray-50 dark:bg-gray-900">
      <header className={cn('flex items-center gap-3 p-3 border-b',
        'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800')}>
        <button type="button" onClick={onBack} aria-label="Back to session"
          className={cn('rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
            'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
          ← Back
        </button>
        <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300 truncate">
          <span className="opacity-80">{parentTitle}</span>
          <span aria-hidden className="mx-1.5 opacity-50">›</span>
          <span className="font-medium">{label}</span>
          {meta?.status === 'running' && (
            <span className="ml-2 text-xs text-amber-600 dark:text-amber-400">running…</span>
          )}
        </nav>
        <span className="ml-auto text-xs text-gray-400 italic">Read-only sub-agent</span>
      </header>
      <main className="flex-1 overflow-hidden flex flex-col">
        {turns.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {meta?.status === 'running' ? 'Sub-agent starting…' : 'No sub-agent activity recorded.'}
            </p>
          </div>
        ) : (
          <TranscriptView turns={turns} permissionMode="interactive"
            onDecision={() => {/* read-only */}} onAnswer={() => {/* read-only */}} />
        )}
      </main>
    </div>
  )
}
```

- [ ] **Step 4: Wire routing**

In `App.tsx`:
- Add to the `View` union: `| { type: 'subSession'; sessionId: string; agentId: string }`.
- Add `const handleOpenSubAgent = useCallback((sessionId: string, agentId: string) => setView({ type: 'subSession', sessionId, agentId }), [])`.
- In the render, add a branch before the dashboard: `view.type === 'subSession' ? <SubSessionView sessionId={view.sessionId} agentId={view.agentId} parentTitle={view.sessionId.slice(0,8)} onBack={handleBack} /> : ...`. (Parent title: pass the real session name if readily available via a lookup; the short id is an acceptable fallback.)
- Pass `onOpenSubAgent={handleOpenSubAgent}` to `<Dashboard>`.
- Import `SubSessionView`.

In `Dashboard.tsx`: add `onOpenSubAgent?: (sessionId: string, agentId: string) => void` to `DashboardProps`, destructure it, and pass `onOpenSubAgent={onOpenSubAgent}` to `<SessionList>` (the active-sessions list).

- [ ] **Step 5: Verify**

Run: `cd web && npx vitest run src/components/session/__tests__/SubSessionView.test.tsx && npx vitest run && npx tsc --noEmit && npx eslint src/components/session/SubSessionView.tsx src/App.tsx src/components/Dashboard.tsx`
Expected: PASS — full web suite green, tsc clean, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/SubSessionView.tsx web/src/App.tsx web/src/components/Dashboard.tsx web/src/components/SessionList.tsx web/src/components/session/__tests__/SubSessionView.test.tsx
git commit -m "feat(web): live sub-session drill-down view + routing"
```

---

## Final Verification

- [ ] **Backend suite:** `cd <worktree> && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/ -q` → all PASS.
- [ ] **Web suite:** `cd web && npx vitest run && npx tsc --noEmit && npx eslint .` → all PASS.
- [ ] **Manual smoke (using the `run`/`verify` skill):** start the app, run a session that spawns a sub-agent, confirm (a) the main thread shows the `Task` card but NOT the sub-agent's internal text/tools, (b) a nested sub-agent row appears under the session in the list with a `running` dot, (c) clicking it opens the sub-agent transcript read-only with a breadcrumb and streams live, (d) deeper nesting nests recursively.
- [ ] **Completion workflow (CLAUDE.md):** push the branch, open a PR with `gh pr create`, and merge once green.

## Spec Coverage Check

- Main thread excludes sub-agent content → Tasks 3 (live tag), 4 (replay tag), 9 (filter).
- Hierarchical sub-sessions in the session list, arbitrary depth → Tasks 6–7 (tree), 10 (recursive rows).
- Open in main pane + breadcrumb → Task 11.
- Live with running status → Tasks 3/4 (`SubAgentUpdate` status), 8 (live stream), 11 (running indicator).
- Derived, no new processes → entire design is attribution over existing events (Tasks 2–4).
- Persistence for archived list nesting (nullable column) → Task 5.
- Verify-against-real-binary gate for replay linkage → Task 1.
