# Rich Session Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the laggy PTY→xterm terminal with a Claude-Code-web-style rich client — streamed message/thinking/tool-card rendering, click-to-approve tool permissions, plan mode, interrupt, queue, AskUserQuestion buttons, and auto-titling — driven by the Python `claude-agent-sdk`.

**Architecture:** A backend `SessionDriver` wraps `claude-agent-sdk`'s `ClaudeSDKClient` (stream-json under the hood, using the per-account composed `CLAUDE_CONFIG_DIR` OAuth). A **translator** maps SDK messages → ccswitch **normalized events**; nothing downstream sees the SDK wire format. The WS bridge relays normalized events; the React frontend rebuilds them into a transcript via an event-sourced reducer. The PTY `SessionManager` and xterm are deleted.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async + aiosqlite, `claude-agent-sdk==0.2.105`; React 18 + Vite + TypeScript, `react-markdown` + `remark-gfm`, Shiki (lazy), Vitest + RTL.

**Spec:** `docs/superpowers/specs/2026-06-20-rich-session-rendering-design.md` (read it first).

## Global Constraints

- **No new code consumes account quota in tests.** All backend tests use a fake SDK client + hand-authored JSONL fixtures. Real `claude` is never spawned in CI.
- **Permission default ships as `interactive`.** `auto`/`allowlist` are opt-in; "full auto (incl. shell)" is selectable (incl. as global default per owner decision) but must show a persistent banner. Even `auto` keeps a dangerous-tool denylist unless full-auto is on.
- **Dangerous-tool denylist (verbatim):** `Bash`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `WebFetch`, `WebSearch`, and any MCP tool not explicitly whitelisted.
- **Per-message WS authorization:** re-verify identity on every inbound WS frame; bind each `permId`/`questionId` to the session creator's identity; drop responses from other identities or for unknown/resolved ids.
- **Output is untrusted:** `react-markdown` with **no `rehype-raw`, no `dangerouslySetInnerHTML`**; Bash stdout/stderr, Read content, and tool inputs render as escaped text (ANSI stripped).
- **Redaction is server-side** over every `textDelta`/`thinkingDelta`/`toolResult` and the JSONL replay path before anything leaves the host.
- **All SDK tool names cross the seam only as a ccswitch `toolKind` enum.** Frontend branches on `toolKind`, never raw names.
- **Event correlation:** every normalized event carries the source SDK message `uuid` (field `srcUuid`) so replay and live merge by id (upsert, never duplicate).
- **Transport hardening is load-bearing:** cookie `secure=True` by default; non-empty `tailnet_allowlist` required outside dev (refuse boot otherwise); bind uvicorn to the tailscale/loopback interface, never `0.0.0.0`; cap inbound WS frames at 256 KB; cap queued prompts; rate-limit `setPermissionMode`.
- **Test commands:** `.venv/bin/python -m unittest discover -s tests` (core), `.venv/bin/python -m pytest server/tests/ -q` (server), and in `web/`: `npm run typecheck && npm run test && npm run build`. All must stay green.
- **Commits:** conventional-commit messages; commit at the end of every task.

---

## File structure

**Backend (new):**
- `server/sessions/events.py` — normalized event dataclasses + `ToolKind` enum + `to_wire()`.
- `server/sessions/sdk_contract.py` — `SDKClientProtocol` (the exact surface `SessionDriver` calls).
- `server/sessions/translator.py` — `translate(sdk_msg) -> list[Event]`.
- `server/sessions/permissions.py` — `PermissionRegistry` (Futures, fingerprint, TTL, allowlist).
- `server/sessions/driver.py` — `SessionDriver` (wraps the SDK client).
- `server/sessions/replay.py` — JSONL transcript → summary events.
- `server/api/settings.py` — `GET/PUT /api/settings`.
- `server/tests/fakes/fake_sdk.py` — fake SDK client; `server/tests/fixtures/*.jsonl` — message fixtures.

**Backend (modified):**
- `server/sessions/orchestrator.py` — manage drivers not PTYs; own the `PermissionRegistry`.
- `server/sessions/rotation.py` — rotate via driver resume.
- `server/bridge/ws.py` — normalized-event bridge (rewrite), per-message authz, frame batching, replay.
- `server/audit.py` — apply redaction to event stream; add permission/mode audit events.
- `server/auth/middleware.py` — `verify_ws_frame_identity`, cookie `secure=True`, allowlist-required boot check.
- `server/db/models.py` — `Session.title`, `Session.permission_mode`, `Session.tool_allowlist`; new `Settings` row.
- `server/app.py` — wire settings router; bind-interface assertion.

**Backend (deleted):** `server/sessions/manager.py` (PTY) once the driver replaces all consumers.

**Frontend (new):**
- `web/src/session/events.ts` — TS event types (mirror of the contract).
- `web/src/session/reducer.ts` — `transcriptReducer`.
- `web/src/components/session/SessionView.tsx`, `TranscriptView.tsx`, `Turn.tsx`, `UserBubble.tsx`, `AssistantText.tsx`, `ThinkingBlock.tsx`, `ToolCallCard.tsx`, `ToolResult.tsx` (+ `DiffView.tsx`, `FileContent.tsx`, `BashResult.tsx`, `TodoList.tsx`), `PermissionCard.tsx`, `QuestionCard.tsx`, `Composer.tsx`, `StatusBar.tsx`, `RotationDivider.tsx`.
- `web/src/components/settings/SettingsPanel.tsx`.

**Frontend (modified):** `web/src/ws/client.ts` (event union + dispatch + frame-array), `web/src/api/hooks.ts` (settings hooks), `web/src/components/Dashboard.tsx` (route to SessionView; permission-mode select in New Session), `web/vitest.config.ts` (`clearMocks: true`).

**Frontend (deleted):** `web/src/components/Terminal.tsx`, `web/src/components/HelperKeyBar.tsx`, `@xterm/*` deps.

---

## Phase 0 — Feasibility spikes (binary gates; do first)

> Spikes are investigations, not TDD. Each ends with a written verdict in `docs/superpowers/spikes/2026-06-20-<name>.md` and (where noted) a committed fixture/contract artifact. **If a gate fails, stop and escalate** — Task 0.2 failing flips the whole driver to Approach B (raw stream-json), which is a different plan.

### Task 0.1: SDK OAuth + persistent streaming spike

**Files:** Create `docs/superpowers/spikes/2026-06-20-sdk-oauth-streaming.md`; create `server/sessions/sdk_contract.py` (first draft).

- [ ] **Step 1: Install the SDK**

Run: `.venv/bin/pip install 'claude-agent-sdk==0.2.105'` then add it to `pyproject.toml`/requirements.

- [ ] **Step 2: Minimal persistent-session probe**

Write a throwaway script `/tmp/spike_sdk.py` that, against a temp git repo and a real composed account config dir (reuse `ccswitch.configdir.compose("<account>")`), opens a `ClaudeSDKClient`, sends two prompts in one session, and prints each streamed message object's `type` and attributes. Run it once manually (this is the ONLY task allowed to spend a little quota).

```python
import asyncio, os
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from ccswitch import configdir

async def main():
    cfg = configdir.compose("kindash")            # composed OAuth config dir
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
    opts = ClaudeAgentOptions(cwd="/tmp/spikerepo", permission_mode="default")
    async with ClaudeSDKClient(options=opts) as c:
        await c.query("say hi in 3 words")
        async for m in c.receive_response():
            print(type(m).__name__, getattr(m, "__dict__", m))
        await c.query("now say bye in 3 words")
        async for m in c.receive_response():
            print(type(m).__name__)
asyncio.run(main())
```

- [ ] **Step 3: Record the verdict + the contract**

In the spike doc, record: PASS/FAIL on "uses OAuth from `CLAUDE_CONFIG_DIR` with no API key", "stays alive for 2+ turns", the exact message class names/fields observed, and the exact methods used (`query`, `receive_response`, `interrupt`, context-manager protocol). Copy the observed surface into `server/sessions/sdk_contract.py` as a `typing.Protocol` named `SDKClientProtocol` with method stubs + docstrings (no implementation).

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/spikes/2026-06-20-sdk-oauth-streaming.md server/sessions/sdk_contract.py pyproject.toml
git commit -m "spike: confirm claude-agent-sdk uses composed OAuth + persistent streaming"
```

**Gate:** OAuth + multi-turn must PASS. If FAIL → switch to Approach B; re-plan Phase 1.

### Task 0.2: Resume-on-rotation spike

**Files:** Create `docs/superpowers/spikes/2026-06-20-resume-on-rotation.md`.

- [ ] **Step 1: Probe cross-config-dir resume**

Extend the throwaway script: run one turn under account A's config dir, capture the `claude_session_id` the SDK reports, close the client, then open a NEW `ClaudeSDKClient` with `options=ClaudeAgentOptions(resume="<that id>", cwd=same)` but `CLAUDE_CONFIG_DIR` = account B's composed dir, and send a follow-up that references the first turn ("what did I just ask?").

- [ ] **Step 2: Record verdict**

Binary criterion: **the account-B client resumes and answers with knowledge of account-A's turn.** Record PASS/FAIL and how the `claude_session_id` is surfaced by the SDK (which message/attribute).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/spikes/2026-06-20-resume-on-rotation.md
git commit -m "spike: confirm SDK resume works across account config dirs (rotation)"
```

**Gate:** Must PASS for Approach A rotation. If FAIL → rotation uses Approach B raw stream-json `--resume` (still feasible; note it in the spike and adjust Task 2.2).

### Task 0.3: AskUserQuestion-in-SDK spike

**Files:** Create `docs/superpowers/spikes/2026-06-20-askuserquestion.md`.

- [ ] **Step 1: Probe**

Prompt the SDK session in a way that reliably triggers `AskUserQuestion` (e.g. "Ask me a multiple-choice question about my preference using the AskUserQuestion tool"). Observe whether it surfaces as a tool call routed to `can_use_tool`/a tool handler we can fulfill, or not at all.

- [ ] **Step 2: Record verdict + decide**

If fulfillable → record the exact mechanism (tool name + how to return the selected option as the tool result). If not → record that `question` events are **deferred**; the model will ask in plain text, and the frontend `QuestionCard` ships but is simply never emitted until a later iteration.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/spikes/2026-06-20-askuserquestion.md
git commit -m "spike: determine AskUserQuestion handling in SDK headless mode"
```

**Gate:** Non-blocking. Outcome decides whether Task 1.3 emits `question` events and Task 3.4 wires them live.

---

## Phase 1 — Backend core (driver, translator, events, permissions)

### Task 1.1: Normalized event types

**Files:** Create `server/sessions/events.py`; Test `server/tests/test_events.py`.

**Interfaces:**
- Produces: `ToolKind` (`Enum`: `EDIT, WRITE, READ, BASH, TODO, SEARCH, UNKNOWN`); `tool_kind(name: str) -> ToolKind`; event dataclasses (`TurnStart, TextDelta, ThinkingDelta, ToolCall, ToolResult, PermissionRequest, Question, Title, Status, Rotated, ErrorEvent, ReplayComplete, TurnEnd`) each with `to_wire() -> dict`. Every event has `src_uuid: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_events.py
from server.sessions.events import ToolKind, tool_kind, TextDelta, ToolCall, Status

def test_tool_kind_maps_known_names():
    assert tool_kind("Edit") is ToolKind.EDIT
    assert tool_kind("Bash") is ToolKind.BASH
    assert tool_kind("Grep") is ToolKind.SEARCH
    assert tool_kind("SomethingNew") is ToolKind.UNKNOWN

def test_textdelta_to_wire():
    e = TextDelta(turn_id="t1", text="hi", src_uuid="u1")
    assert e.to_wire() == {"type": "textDelta", "turnId": "t1", "text": "hi", "srcUuid": "u1"}

def test_status_to_wire_is_structured():
    assert Status(state="queued", count=2).to_wire() == {"type": "status", "state": "queued", "count": 2}

def test_toolcall_carries_kind_and_turn():
    e = ToolCall(turn_id="t1", tool_id="x", name="Edit", kind=ToolKind.EDIT, input={"file_path": "a"}, src_uuid="u2")
    w = e.to_wire()
    assert w["type"] == "toolCall" and w["toolKind"] == "edit" and w["turnId"] == "t1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_events.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `events.py`**

```python
# server/sessions/events.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class ToolKind(str, Enum):
    EDIT = "edit"; WRITE = "write"; READ = "read"; BASH = "bash"
    TODO = "todo"; SEARCH = "search"; UNKNOWN = "unknown"

_TOOL_KIND = {
    "Edit": ToolKind.EDIT, "MultiEdit": ToolKind.EDIT, "NotebookEdit": ToolKind.EDIT,
    "Write": ToolKind.WRITE, "Read": ToolKind.READ, "Bash": ToolKind.BASH,
    "TodoWrite": ToolKind.TODO, "Grep": ToolKind.SEARCH, "Glob": ToolKind.SEARCH,
    "LS": ToolKind.SEARCH,
}
def tool_kind(name: str) -> ToolKind:
    return _TOOL_KIND.get(name, ToolKind.UNKNOWN)

@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id, "srcUuid": self.src_uuid}

@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; src_uuid: str | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage, "srcUuid": self.src_uuid}

@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid}

@dataclass
class ThinkingDelta:
    turn_id: str; text: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "thinkingDelta", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid}

@dataclass
class ToolCall:
    turn_id: str; tool_id: str; name: str; kind: ToolKind; input: dict; src_uuid: str | None = None
    def to_wire(self): return {"type": "toolCall", "turnId": self.turn_id, "toolId": self.tool_id,
                               "name": self.name, "toolKind": self.kind.value, "input": self.input, "srcUuid": self.src_uuid}

@dataclass
class ToolResult:
    tool_id: str; ok: bool; payload: dict; src_uuid: str | None = None
    def to_wire(self): return {"type": "toolResult", "toolId": self.tool_id, "ok": self.ok, "payload": self.payload, "srcUuid": self.src_uuid}

@dataclass
class PermissionRequest:
    perm_id: str; turn_id: str; tool: str; kind: ToolKind; input: dict; plan: bool = False
    def to_wire(self): return {"type": "permissionRequest", "permId": self.perm_id, "turnId": self.turn_id,
                               "tool": self.tool, "toolKind": self.kind.value, "input": self.input, "plan": self.plan}

@dataclass
class Question:
    question_id: str; turn_id: str; header: str; question: str; options: list[dict]; multi_select: bool = False
    def to_wire(self): return {"type": "question", "questionId": self.question_id, "turnId": self.turn_id,
                               "header": self.header, "question": self.question, "options": self.options, "multiSelect": self.multi_select}

@dataclass
class Title:
    title: str; source: str
    def to_wire(self): return {"type": "title", "title": self.title, "source": self.source}

@dataclass
class Status:
    state: str; count: int = 0
    def to_wire(self): return {"type": "status", "state": self.state, "count": self.count}

@dataclass
class Rotated:
    from_account: str; to_account: str; reason: str; turn_restarted: bool
    def to_wire(self): return {"type": "rotated", "from": self.from_account, "to": self.to_account,
                               "reason": self.reason, "turnRestarted": self.turn_restarted}

@dataclass
class ErrorEvent:
    code: str; message: str; resets_at: str | None = None
    def to_wire(self): return {"type": "error", "code": self.code, "message": self.message, "resetsAt": self.resets_at}

@dataclass
class ReplayComplete:
    def to_wire(self): return {"type": "replayComplete"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_events.py -q` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events.py
git commit -m "feat(sessions): normalized event types + ToolKind enum"
```

### Task 1.2: Fake SDK client + fixtures

**Files:** Create `server/tests/fakes/fake_sdk.py`, `server/tests/fixtures/simple_turn.json`, `server/tests/fixtures/tool_turn.json`; Test `server/tests/test_fake_sdk.py`.

**Interfaces:**
- Consumes: `SDKClientProtocol` from Task 0.1.
- Produces: `FakeSDKClient(messages: list[dict], on_can_use_tool=None)` implementing `SDKClientProtocol`: async ctx manager, `query(text)`, `receive_response()` (async iterator yielding objects whose `.kind`/`.data` mirror the SDK's observed shape from the spike), `interrupt()`, `set_permission_mode(mode)`. `messages` are loaded from a fixture JSON (a list of `{kind, data}` records modeled on the spike's observed message classes).

- [ ] **Step 1: Write fixtures** (hand-authored, modeled on Task 0.1's observed shapes). `simple_turn.json`:

```json
[
  {"kind": "system", "data": {"session_id": "sess-1", "uuid": "u0"}},
  {"kind": "assistant_text_delta", "data": {"uuid": "u1", "text": "Hello "}},
  {"kind": "assistant_text_delta", "data": {"uuid": "u1", "text": "there"}},
  {"kind": "result", "data": {"uuid": "u1", "usage": {"output_tokens": 3}}}
]
```

`tool_turn.json`:

```json
[
  {"kind": "system", "data": {"session_id": "sess-1", "uuid": "u0"}},
  {"kind": "assistant_text_delta", "data": {"uuid": "u1", "text": "I'll read it."}},
  {"kind": "tool_use", "data": {"uuid": "u2", "tool_id": "tc1", "name": "Read", "input": {"file_path": "a.py"}}},
  {"kind": "tool_result", "data": {"uuid": "u3", "tool_id": "tc1", "ok": true, "content": "print(1)\n"}},
  {"kind": "result", "data": {"uuid": "u4", "usage": {}}}
]
```

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_fake_sdk.py
import json, asyncio
from pathlib import Path
from server.tests.fakes.fake_sdk import FakeSDKClient

FIX = Path(__file__).parent / "fixtures"

def test_fake_replays_messages_in_order():
    msgs = json.loads((FIX / "simple_turn.json").read_text())
    async def run():
        seen = []
        async with FakeSDKClient(msgs) as c:
            await c.query("hi")
            async for m in c.receive_response():
                seen.append(m.kind)
        return seen
    assert asyncio.run(run()) == ["system", "assistant_text_delta", "assistant_text_delta", "result"]
```

- [ ] **Step 3: Run to verify fail** → `pytest server/tests/test_fake_sdk.py -q` → FAIL.

- [ ] **Step 4: Implement the fake**

```python
# server/tests/fakes/fake_sdk.py
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class FakeMsg:
    kind: str
    data: dict

class FakeSDKClient:
    def __init__(self, messages, on_can_use_tool=None):
        self._messages = messages
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.sent: list[str] = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def query(self, text: str): self.sent.append(text)
    async def receive_response(self):
        for m in self._messages:
            if self.interrupted: break
            yield FakeMsg(kind=m["kind"], data=m["data"])
    async def interrupt(self): self.interrupted = True
    async def set_permission_mode(self, mode: str): self.permission_mode = mode
```

- [ ] **Step 5: Run → PASS.** Commit:

```bash
git add server/tests/fakes/fake_sdk.py server/tests/fixtures/ server/tests/test_fake_sdk.py
git commit -m "test(sessions): fake SDK client + JSONL fixtures (no quota)"
```

### Task 1.3: Translator (SDK message → normalized events)

**Files:** Create `server/sessions/translator.py`; Test `server/tests/test_translator.py`.

**Interfaces:**
- Consumes: `FakeMsg`-shaped objects (`.kind`, `.data`), `events.*`, `events.tool_kind`.
- Produces: `class Translator` with `feed(msg) -> list[Event]` (stateful: tracks current `turn_id` from the first assistant message uuid). On `system` → records `session_id` (exposed as `.session_id`). On first assistant content of a turn → emits `TurnStart`. On `result` → emits `TurnEnd`. Maps `assistant_text_delta`→`TextDelta`, `thinking_delta`→`ThinkingDelta`, `tool_use`→`ToolCall`, `tool_result`→`ToolResult`. `Translator.session_id: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_translator.py
import json
from pathlib import Path
from server.sessions.translator import Translator
from server.sessions.events import TurnStart, TextDelta, ToolCall, ToolResult, TurnEnd, ToolKind
from server.tests.fakes.fake_sdk import FakeMsg

FIX = Path(__file__).parent / "fixtures"
def _feed(name):
    tr = Translator(); out = []
    for m in json.loads((FIX / name).read_text()):
        out += tr.feed(FakeMsg(m["kind"], m["data"]))
    return tr, out

def test_simple_turn_emits_start_deltas_end():
    tr, out = _feed("simple_turn.json")
    assert tr.session_id == "sess-1"
    types = [type(e).__name__ for e in out]
    assert types == ["TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
    assert out[1].text == "Hello " and out[1].turn_id == out[0].turn_id

def test_tool_turn_emits_toolcall_with_kind_and_result():
    _, out = _feed("tool_turn.json")
    call = next(e for e in out if isinstance(e, ToolCall))
    res = next(e for e in out if isinstance(e, ToolResult))
    assert call.kind is ToolKind.READ and call.tool_id == "tc1"
    assert res.tool_id == "tc1" and res.ok is True and res.payload["content"] == "print(1)\n"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `translator.py`**

```python
# server/sessions/translator.py
from __future__ import annotations
from . import events as ev

class Translator:
    def __init__(self):
        self.session_id: str | None = None
        self._turn_id: str | None = None
        self._started = False

    def _ensure_turn(self, uuid):
        out = []
        if not self._started:
            self._turn_id = self._turn_id or uuid or "turn"
            out.append(ev.TurnStart(turn_id=self._turn_id, src_uuid=uuid))
            self._started = True
        return out

    def feed(self, msg) -> list:
        k, d = msg.kind, msg.data
        if k == "system":
            self.session_id = d.get("session_id") or self.session_id
            return []
        if k == "assistant_text_delta":
            out = self._ensure_turn(d.get("uuid"))
            out.append(ev.TextDelta(self._turn_id, d["text"], d.get("uuid")))
            return out
        if k == "thinking_delta":
            out = self._ensure_turn(d.get("uuid"))
            out.append(ev.ThinkingDelta(self._turn_id, d["text"], d.get("uuid")))
            return out
        if k == "tool_use":
            out = self._ensure_turn(d.get("uuid"))
            out.append(ev.ToolCall(self._turn_id, d["tool_id"], d["name"],
                                   ev.tool_kind(d["name"]), d.get("input", {}), d.get("uuid")))
            return out
        if k == "tool_result":
            return [ev.ToolResult(d["tool_id"], d.get("ok", True),
                                  {k2: v for k2, v in d.items() if k2 not in ("tool_id", "ok", "uuid")},
                                  d.get("uuid"))]
        if k == "result":
            tid = self._turn_id or "turn"
            self._started = False; self._turn_id = None
            return [ev.TurnEnd(tid, d.get("usage"), d.get("uuid"))]
        return []
```

- [ ] **Step 4: Run → PASS.** Commit:

```bash
git add server/sessions/translator.py server/tests/test_translator.py
git commit -m "feat(sessions): SDK→normalized-event translator"
```

### Task 1.4: PermissionRegistry (externalized Futures, fingerprint, TTL, allowlist)

**Files:** Create `server/sessions/permissions.py`; Test `server/tests/test_permissions.py`.

**Interfaces:**
- Produces: `class PermissionRegistry`:
  - `fingerprint(tool: str, input: dict) -> str` (Bash → first token + cwd; Edit/Write → abs path; else tool name).
  - `async request(session_id, turn_id, tool, input, *, mode, denylist, allowlist, identity) -> tuple[str|None, asyncio.Future|None]` — returns `(perm_id, future)` when a round-trip is needed, or `(None, None)` when auto-resolved (the caller awaits the future for `True`/`False`; auto-resolved means immediate allow).
  - `resolve(perm_id, decision, identity) -> bool` — `decision ∈ {allow, deny, always}`; rejects mismatched identity / unknown id (returns `False`); `always` records a fingerprinted grant with TTL.
  - `is_granted(session_id, tool, input, now) -> bool`.
  - `pending(session_id) -> list[dict]` (for reattach re-emit).
  - `deny_all(session_id)` (interrupt/disconnect/close).
  - `DANGEROUS: frozenset` constant.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_permissions.py
import asyncio, time
from server.sessions.permissions import PermissionRegistry

def test_interactive_mode_round_trips_then_allow():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls", "cwd": "/x"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        assert pid is not None
        reg.resolve(pid, "allow", "me")
        return await fut
    assert asyncio.run(run()) is True

def test_auto_mode_denylist_still_round_trips_bash():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls"},
                                     mode="auto", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        return pid
    assert asyncio.run(run()) is not None   # denylisted → still prompts

def test_full_auto_allows_bash_without_prompt():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls"},
                                     mode="full-auto", denylist=frozenset(),
                                     allowlist=True, identity="me")
        return pid, (await fut if fut else True)
    pid, allowed = asyncio.run(run())
    assert pid is None and allowed is True

def test_always_grant_is_fingerprinted_and_expires():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "npm test", "cwd": "/r"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        reg.resolve(pid, "always", "me")
        await fut
    asyncio.run(run())
    now = time.time()
    assert reg.is_granted("s1", "Bash", {"command": "npm test", "cwd": "/r"}, now) is True
    assert reg.is_granted("s1", "Bash", {"command": "rm -rf /", "cwd": "/r"}, now) is False  # different fingerprint
    assert reg.is_granted("s1", "Bash", {"command": "npm test", "cwd": "/r"}, now + 4000) is False  # TTL

def test_resolve_rejects_wrong_identity_and_unknown():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Edit", {"file_path": "/a"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="owner")
        assert reg.resolve(pid, "allow", "attacker") is False
        assert reg.resolve("nope", "allow", "owner") is False
        reg.resolve(pid, "deny", "owner")
        return await fut
    assert asyncio.run(run()) is False

def test_deny_all_resolves_pending_false():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Edit", {"file_path": "/a"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="o")
        reg.deny_all("s1")
        return await fut
    assert asyncio.run(run()) is False
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `permissions.py`**

```python
# server/sessions/permissions.py
from __future__ import annotations
import asyncio, time, uuid as _uuid

class PermissionRegistry:
    DANGEROUS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"})
    GRANT_TTL = 3600  # seconds

    def __init__(self):
        self._pending: dict[str, dict] = {}            # perm_id -> {future, identity, session_id, meta}
        self._grants: dict[tuple[str, str], float] = {}  # (session_id, fingerprint) -> expires_at

    @staticmethod
    def fingerprint(tool: str, input: dict) -> str:
        if tool == "Bash":
            cmd = (input.get("command") or "").strip().split()
            return f"Bash:{cmd[0] if cmd else ''}:{input.get('cwd','')}"
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return f"{tool}:{input.get('file_path') or input.get('notebook_path','')}"
        return tool

    def is_granted(self, session_id, tool, input, now) -> bool:
        exp = self._grants.get((session_id, self.fingerprint(tool, input)))
        return exp is not None and exp > now

    async def request(self, session_id, turn_id, tool, input, *, mode, denylist, allowlist, identity):
        now = time.time()
        # auto-resolve paths
        if mode == "full-auto":
            return None, _resolved(True)
        if allowlist and self.is_granted(session_id, tool, input, now):
            return None, _resolved(True)
        if mode in ("auto", "allowlist") and tool not in denylist:
            return None, _resolved(True)
        # round-trip
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "turn_id": turn_id}
        return pid, fut

    def resolve(self, perm_id, decision, identity) -> bool:
        rec = self._pending.get(perm_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[perm_id]
        if decision == "always":
            fp = self.fingerprint(rec["tool"], rec["input"])
            self._grants[(rec["session_id"], fp)] = time.time() + self.GRANT_TTL
            decision = "allow"
        if not rec["future"].done():
            rec["future"].set_result(decision == "allow")
        return True

    def pending(self, session_id) -> list[dict]:
        return [{"permId": pid, **{k: r[k] for k in ("tool", "input", "turn_id")}}
                for pid, r in self._pending.items() if r["session_id"] == session_id]

    def deny_all(self, session_id):
        for pid in [p for p, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(pid)
            if not rec["future"].done():
                rec["future"].set_result(False)

def _resolved(value: bool):
    fut = asyncio.get_event_loop().create_future(); fut.set_result(value); return fut
```

- [ ] **Step 4: Run → PASS.** Commit:

```bash
git add server/sessions/permissions.py server/tests/test_permissions.py
git commit -m "feat(sessions): PermissionRegistry — fingerprinted grants, TTL, identity-bound futures"
```

### Task 1.5: SessionDriver

**Files:** Create `server/sessions/driver.py`; Test `server/tests/test_driver.py`.

**Interfaces:**
- Consumes: `SDKClientProtocol`, `Translator`, `PermissionRegistry`, `events.*`.
- Produces: `class SessionDriver`:
  - `__init__(self, session_id, account, cwd, *, mode, sdk_factory, registry, identity, denylist=PermissionRegistry.DANGEROUS, resume=None)` — `sdk_factory(cwd, config_dir, can_use_tool, permission_mode, resume) -> SDKClientProtocol` (injected; production factory builds the real `ClaudeSDKClient`).
  - `async start()` — opens the client; sets `.claude_session_id` from the translator's `session_id`.
  - `events() -> AsyncIterator[Event]` — the normalized out-stream (drains an internal `asyncio.Queue`).
  - `async send_prompt(text)`, `async interrupt()`, `async set_permission_mode(mode)`, `resolve_permission(perm_id, decision, identity)`, `async aclose()`.
  - The `can_use_tool` callback bridges to `registry.request(...)`, emits a `PermissionRequest` event, awaits the future, returns the SDK's allow/deny shape.

- [ ] **Step 1: Write the failing test** (drives a full turn + a permission round-trip with the fake)

```python
# server/tests/test_driver.py
import json, asyncio
from pathlib import Path
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnStart, TextDelta, TurnEnd, ToolCall
from server.tests.fakes.fake_sdk import FakeSDKClient

FIX = Path(__file__).parent / "fixtures"

def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, permission_mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make

def test_driver_streams_turn_events():
    msgs = json.loads((FIX / "simple_turn.json").read_text())
    async def run():
        d = SessionDriver("s1", "kindash", "/tmp/r", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("hi")
        seen = []
        async for e in d.events():
            seen.append(type(e).__name__)
            if isinstance(e, TurnEnd): break
        await d.aclose()
        assert d.claude_session_id == "sess-1"
        return seen
    assert asyncio.run(run()) == ["TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `driver.py`** (full code — this is the core; include the permission bridge, the event pump, and interrupt-denies-pending behavior)

```python
# server/sessions/driver.py
from __future__ import annotations
import asyncio, logging
from . import events as ev
from .translator import Translator
from .permissions import PermissionRegistry

log = logging.getLogger(__name__)

class SessionDriver:
    def __init__(self, session_id, account, cwd, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None):
        self.session_id = session_id
        self.account = account
        self.cwd = cwd
        self.mode = mode
        self.identity = identity
        self.denylist = denylist
        self.resume = resume
        self.claude_session_id: str | None = None
        self._factory = sdk_factory
        self._reg = registry
        self._tr = Translator()
        self._client = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None

    async def _can_use_tool(self, tool_name, input, _ctx=None):
        perm_id, fut = await self._reg.request(
            self.session_id, self._tr._turn_id or "turn", tool_name, input,
            mode=self.mode, denylist=self.denylist, allowlist=True, identity=self.identity)
        if perm_id is not None:
            await self._out.put(ev.PermissionRequest(
                perm_id, self._tr._turn_id or "turn", tool_name, ev.tool_kind(tool_name),
                input, plan=(tool_name == "ExitPlanMode")))
        allowed = await fut
        return {"behavior": "allow" if allowed else "deny",
                "message": None if allowed else "denied by user"}

    async def start(self):
        from ccswitch import configdir
        cfg = configdir.compose(self.account)
        configdir.trust_project(self.account, self.cwd)
        self._client = self._factory(self.cwd, str(cfg), self._can_use_tool, self.mode, self.resume)
        await self._client.__aenter__()

    async def send_prompt(self, text: str):
        await self._out.put(ev.Status(state="working"))
        await self._client.query(text)
        self._pump = asyncio.create_task(self._drain())

    async def _drain(self):
        try:
            async for msg in self._client.receive_response():
                for e in self._tr.feed(msg):
                    await self._out.put(e)
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
        except Exception:
            log.exception("driver.drain session=%s", self.session_id)
            await self._out.put(ev.ErrorEvent("driver_error", "session stream failed"))
        finally:
            await self._out.put(ev.Status(state="idle"))

    async def events(self):
        while True:
            yield await self._out.get()

    async def interrupt(self):
        self._reg.deny_all(self.session_id)
        if self._client:
            await self._client.interrupt()

    async def set_permission_mode(self, mode: str):
        self.mode = mode
        if self._client:
            await self._client.set_permission_mode("plan" if mode == "plan" else "default")

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def aclose(self):
        self._reg.deny_all(self.session_id)
        if self._pump:
            self._pump.cancel()
        if self._client:
            await self._client.__aexit__(None, None, None)
```

(Note: the production `sdk_factory` lives in Task 2.1; it constructs the real `ClaudeSDKClient(options=ClaudeAgentOptions(cwd=..., resume=..., permission_mode=..., can_use_tool=...))` per the Task 0.1 contract.)

- [ ] **Step 4: Run → PASS.** Commit:

```bash
git add server/sessions/driver.py server/tests/test_driver.py
git commit -m "feat(sessions): SessionDriver — event pump + permission bridge"
```

### Task 1.6: JSONL transcript replay reader

**Files:** Create `server/sessions/replay.py`; Test `server/tests/test_replay.py`.

**Interfaces:**
- Produces: `async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list[Event]` — reads the JSONL off-thread (`asyncio.to_thread`), parses claude transcript lines, emits **completed-turn summary events** (one `UserBubble`-equivalent per user message via a `TextDelta` on a synthetic user turn is NOT used; instead emit a compact `to_wire` summary): for each user message → a `{"type":"user", ...}` wire dict; for each assistant message → `TurnStart` + a single `TextDelta` with the full text + `ToolCall`/`ToolResult` for each block + `TurnEnd`. Keep only the last `max_turns`. (Deltas are NOT replayed token-by-token.)
- Add a `UserMessage` event to `events.py` (`to_wire` → `{"type":"user","turnId","text","srcUuid"}`) as part of this task.

- [ ] **Step 1: Write a fixture transcript** `server/tests/fixtures/transcript.jsonl` (3 lines: a user msg, an assistant msg with text + a tool_use + tool_result blocks). Use the real claude schema keys (`type`, `message.content[]`, `uuid`).

```jsonl
{"type":"user","uuid":"a1","message":{"role":"user","content":[{"type":"text","text":"read a.py"}]}}
{"type":"assistant","uuid":"a2","message":{"role":"assistant","content":[{"type":"text","text":"Reading."},{"type":"tool_use","id":"tc1","name":"Read","input":{"file_path":"a.py"}}]}}
{"type":"user","uuid":"a3","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tc1","content":"print(1)\n"}]}}
```

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_replay.py
import asyncio
from pathlib import Path
from server.sessions.replay import replay_events
from server.sessions.events import TurnStart, TextDelta, ToolCall, ToolResult, TurnEnd

FIX = Path(__file__).parent / "fixtures" / "transcript.jsonl"
def test_replay_emits_summary_events():
    out = asyncio.run(replay_events(str(FIX)))
    wires = [e.to_wire()["type"] for e in out]
    assert wires[0] == "user"
    assert "turnStart" in wires and "toolCall" in wires and "toolResult" in wires and "turnEnd" in wires
    tc = next(e for e in out if isinstance(e, ToolCall))
    assert tc.name == "Read"
```

- [ ] **Step 3: Run → FAIL.**

- [ ] **Step 4: Implement `replay.py`** (and add `UserMessage` to `events.py`).

```python
# server/sessions/replay.py
from __future__ import annotations
import json, asyncio
from . import events as ev

def _parse(path: str, max_turns: int) -> list:
    lines = [json.loads(l) for l in open(path, errors="ignore") if l.strip()]
    # keep tail
    out: list = []
    for o in lines:
        typ = o.get("type"); msg = o.get("message") or {}; content = msg.get("content") or []
        uuid = o.get("uuid")
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = [b for b in content if b.get("type") == "tool_result"]
            if text:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid))
            for r in results:
                out.append(ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                                         {"content": r.get("content")}, uuid))
        elif typ == "assistant":
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            for b in content:
                if b.get("type") == "text":
                    out.append(ev.TextDelta(uuid, b["text"], uuid))
                elif b.get("type") == "thinking":
                    out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid))
                elif b.get("type") == "tool_use":
                    out.append(ev.ToolCall(uuid, b["id"], b["name"], ev.tool_kind(b["name"]),
                                           b.get("input", {}), uuid))
            out.append(ev.TurnEnd(uuid, None, uuid))
    return out

async def replay_events(jsonl_path: str, *, max_turns: int = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
```

Add to `events.py`:

```python
@dataclass
class UserMessage:
    turn_id: str; text: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "user", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid}
```

- [ ] **Step 5: Run → PASS.** Commit:

```bash
git add server/sessions/replay.py server/sessions/events.py server/tests/test_replay.py server/tests/fixtures/transcript.jsonl
git commit -m "feat(sessions): JSONL transcript replay → summary events"
```

---

## Phase 2 — Backend integration

### Task 2.1: Orchestrator drives `SessionDriver`; production SDK factory

**Files:** Modify `server/sessions/orchestrator.py`; Create `server/sessions/sdk_factory.py`; Test `server/tests/test_orchestrator_driver.py`.

**Interfaces:**
- Produces: `sdk_factory.real_sdk_factory(cwd, config_dir, can_use_tool, permission_mode, resume)` building the real `ClaudeSDKClient` (per Task 0.1 contract). `SessionOrchestrator` now: owns one `PermissionRegistry`; `create_session(...)` builds a `SessionDriver` (injectable `sdk_factory`, default = real), `await driver.start()`, stores it in `self._drivers[session_id]`, persists `claude_session_id`/title; exposes `driver(session_id)`, `resolve_permission(...)`, `set_permission_mode(...)`. Keeps the worktree/clone/trust logic already present (`create_session` cwd/branch path is unchanged from current code).

- [ ] **Step 1: Write the failing test** (inject the fake factory; assert a session creates a driver and streams)

```python
# server/tests/test_orchestrator_driver.py — uses the fake factory from test_driver
# create_session(... sdk_factory=fake) → orch.driver(id) is not None; first event is Status(working) or TurnStart
```
(Write a concrete test mirroring `test_driver.py`'s factory, asserting `orch._drivers[sid]` exists and `driver.claude_session_id` populates after a turn.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the orchestrator changes + `sdk_factory.py`. Replace PTY spawn with driver start; keep account selection, worktree creation, `trust_project`, DB row. Remove `_find_claude_session_id` filesystem scan; read `driver.claude_session_id`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(sessions): orchestrator manages SessionDrivers + real SDK factory`.

### Task 2.2: Rotation mid-turn via driver resume

**Files:** Modify `server/sessions/rotation.py`, `server/sessions/orchestrator.py`; Test `server/tests/test_rotation_driver.py`.

**Interfaces:**
- Produces: `async def rotate(record, deps)` updated for drivers — `deps` provides `interrupt`, `pick_account`, `new_driver(account, resume)`. Flow: `driver.interrupt()` → `registry.deny_all(sid)` → pick account Y → build+start new driver with `resume=claude_session_id` → re-send the interrupted prompt → emit `Rotated(reason="session limit", turn_restarted=True)`.

- [ ] Steps: failing test (fake factory; simulate ceiling; assert new driver started with `resume` set + `Rotated` event emitted + pending permissions denied) → implement → pass → commit `feat(sessions): rotation via driver resume`.

(If Task 0.2 FAILED: implement rotation with Approach B raw stream-json resume here instead; the event contract is unchanged.)

### Task 2.3: Settings + DB columns

**Files:** Modify `server/db/models.py`; Create `server/api/settings.py`; Modify `server/app.py`, `server/api/sessions.py`; Test `server/tests/test_settings.py`.

**Interfaces:**
- Produces: `Session.title: str|None`, `Session.permission_mode: str` (default `"interactive"`), `Session.tool_allowlist: JSON` (default `[]`). New `Settings` single-row model: `default_permission_mode` (default `"interactive"`), `thinking_collapsed` (bool, default True), `repo_root` (str). `GET /api/settings`, `PUT /api/settings`. `CreateSessionRequest` gains `permission_mode: str | None` (falls back to settings default). `create_app` adds an assert that uvicorn host is not `0.0.0.0` unless `CCSWITCH_DEV=1`.

- [ ] Steps: failing test (`GET /api/settings` returns defaults; `PUT` round-trips; new session inherits default mode) → implement (note: `Base.metadata.create_all` recreates dev DB; document that existing DBs need the columns — for the single-user SQLite, deleting the dev DB is acceptable, state it) → pass → commit `feat(api): settings endpoint + session permission_mode/title/allowlist columns`.

### Task 2.4: WS bridge rewrite (normalized events, per-message authz, batching, replay)

**Files:** Rewrite `server/bridge/ws.py`; Modify `server/auth/middleware.py` (add `verify_ws_frame_identity`); Test `server/tests/test_ws.py` (rewrite).

**Interfaces:**
- Consumes: orchestrator `driver(sid)`, `replay_events`, `registry`, `verify_ws_frame_identity`.
- Produces: WS protocol — on `attach`: `verify_ws_identity` (handshake), then `replay_events(jsonl)` → send each `to_wire()` (as a single batched array), then `{"type":"replayComplete"}`, then re-emit `registry.pending(sid)` as `permissionRequest` frames, then stream live `driver.events()` coalesced into ~16ms **arrays** (`{"type":"batch","events":[...]}`). Inbound frames (`prompt`/`interrupt`/`permissionResponse`/`setPermissionMode`/`answerQuestion`): each re-runs `verify_ws_frame_identity(ws)`; reject (close 1008) on mismatch; enforce 256 KB frame cap; `permissionResponse` → `orchestrator.resolve_permission(permId, decision, identity)`; queue `prompt` when status≠idle.

- [ ] Steps: failing tests (rewrite `test_ws.py` to drive the fake-factory orchestrator: attach → receive replay frames → `replayComplete` → send prompt → receive `batch` with `turnStart`/`textDelta`/`turnEnd`; permission round-trip; frame >256KB rejected; identity mismatch closes) → implement → pass → commit `feat(ws): normalized-event bridge with per-frame authz, batching, replay`.

### Task 2.5: Server-side redaction + audit

**Files:** Modify `server/audit.py`, `server/bridge/ws.py`; Test `server/tests/test_redaction_stream.py`.

**Interfaces:**
- Produces: `audit.redact_event(wire: dict) -> dict` applying `_redact` to all string fields recursively (covers `textDelta.text`, `thinkingDelta.text`, `toolResult.payload.*`, replay output). Extend `_REDACT_PATTERNS` with the OAuth refresh-token shape and the `ccswitch_session` cookie shape. `record_audit` calls for `permission.allow|deny|always` (tool + fingerprint + identity + sessionId), `permission_mode.change` (old→new+identity), `interrupt`. WS bridge runs `redact_event` on every outbound frame.

- [ ] Steps: failing test (a `toolResult` payload containing `sk-ant-...` and a 64-hex token is redacted before send; a permission resolve emits an audit row) → implement → pass → commit `feat(security): redact event stream + audit permission decisions`.

### Task 2.6: Input validation + transport hardening

**Files:** Modify `src/ccswitch/worktrees.py`, `server/api/sessions.py`, `server/auth/middleware.py`, `server/auth/identity.py`, `server/app.py`; Tests in `tests/test_worktrees.py`, `server/tests/test_api.py`.

**Interfaces:**
- Produces: `git worktree add … -- <branch>` and reject branch names starting with `-`; validate `account` against `paths.account_cfg_dir` existence; validate `repo` as `^[\w.-]+/[\w.-]+$`; cookie `secure=True` default (`CCSWITCH_DEV_INSECURE_COOKIE=1` opt-out); boot check: refuse to start if `tailnet_allowlist` empty and not dev; `clone_path`/worktree path `.resolve()` containment assert.

- [ ] Steps: failing tests (branch `-x` rejected with 400; empty-allowlist boot raises; cookie secure flag asserted) → implement → pass → commit `feat(security): input validation + transport hardening`.

---

## Phase 3 — Frontend

### Task 3.1: WS client event union + frame-array dispatch

**Files:** Modify `web/src/ws/client.ts`; Create `web/src/session/events.ts`; Test `web/src/ws/client.test.ts` (extend).

**Interfaces:**
- Produces: `web/src/session/events.ts` — TS discriminated union mirroring the wire contract (`TurnStartMsg`, `TextDeltaMsg`, … `BatchMsg {type:'batch', events: SessionEvent[]}`). `client.ts` `EventMap` extended with `sessionEvent: (e: SessionEvent) => void`; `_handleInbound` handles `{type:'batch'}` by iterating `events` and emitting each as `sessionEvent`; single non-batch events also dispatch as `sessionEvent`. Outbound helpers: `prompt`, `interrupt`, `permissionResponse`, `answerQuestion`, `setPermissionMode`.

- [ ] Steps: failing test (a `batch` frame fans out N `sessionEvent` callbacks in order; `prompt('hi')` serializes `{type:'prompt',sessionId,text:'hi'}`) → implement → pass → commit `feat(web): session event union + batch dispatch on the WS client`.

### Task 3.2: Transcript reducer

**Files:** Create `web/src/session/reducer.ts`; Test `web/src/session/reducer.test.ts`.

**Interfaces:**
- Produces: `transcriptReducer(state, event) -> state` where `state = { turns: Turn[], openBlockId: string|null, status, queued: QueuedPrompt[], pendingPermissions: Record<string,Permission>, pendingQuestions: ... }`. `Turn = { id, role, blocks: Block[] }`. Block types: `text|thinking|tool|permission|question`. Rules: `textDelta`/`thinkingDelta` append to the open block (create if `openBlockId` null); `toolCall` closes the open text block (`openBlockId=null`) and appends a `tool` block keyed by `toolId`; `toolResult` upserts onto the matching `tool` block by `toolId` (orphan result → ignored, no crash); `turnEnd` clears `openBlockId`; `permissionRequest`/`question` upsert by `permId`/`questionId` into the named turn; resolving a permission removes its block; `status` replaces status; **all upserts are by id (idempotent for replay)**.

- [ ] Steps: failing tests — enumerate the spec's required cases: (a) delta into open turn appends; (b) `toolCall` mid-text closes text block then next `textDelta` opens new block; (c) orphan `toolResult` no-crash; (d) `permissionRequest` then `turnEnd` without response keeps the card; (e) duplicate event by `srcUuid`/id is idempotent (replay-safe); (f) `status` queued count. → implement → pass → commit `feat(web): event-sourced transcript reducer`.

### Task 3.3: Block renderers

**Files:** Create `web/src/components/session/{UserBubble,AssistantText,ThinkingBlock,ToolCallCard,ToolResult,DiffView,FileContent,BashResult,TodoList}.tsx`; Tests alongside in `__tests__/`.

**Interfaces:**
- Produces: `AssistantText({text, streaming})` — renders **plain `<pre>` while `streaming`**, switches to `react-markdown` (no `rehype-raw`) + lazy Shiki on `streaming=false`; Shiki pinned to `['typescript','javascript','tsx','python','bash','json','markdown','rust','go']` + one dark theme; Suspense fallback = plain `<pre>`. `BashResult`/`FileContent` render escaped text in `<pre>` (ANSI stripped via a small `stripAnsi`). `ToolCallCard({kind,name,input})` — header (icon by `toolKind` + key arg) + expandable input (`<button aria-expanded>`); `min-h-[44px]`. `DiffView` caps at first/last 100 lines + "show all".

- [ ] Steps per component: failing RTL test (e.g. `AssistantText` streaming renders raw text, settled renders highlighted; `BashResult` escapes `<script>`; `DiffView` truncates >200 lines) → implement → pass. Commit once per 2-3 components: `feat(web): transcript block renderers`.

### Task 3.4: PermissionCard + QuestionCard

**Files:** Create `web/src/components/session/{PermissionCard,QuestionCard}.tsx`; Tests alongside.

**Interfaces:**
- Produces: `PermissionCard({req, onDecision})` — buttons Yes/No/"Yes, always" (plan variant: "Approve & run"/"Keep planning"); **moves focus to the primary button on mount**; stacks vertically ≥48px on narrow viewports; `aria-label` includes tool + key arg; "Yes, always" is a visually heavier/distinct affordance. `QuestionCard({q, onAnswer})` — single-select = `role="radiogroup"` (auto-submit on choose); multi-select = `role="group"` checkboxes + explicit Submit. On any resolution, focus returns to the composer (via an `onResolved` focus callback).

- [ ] Steps: failing RTL tests (focus lands on primary button on mount; multi-select needs Submit; single-select auto-submits; `aria-label` contains tool name) → implement → pass → commit `feat(web): permission + question cards (focus-managed, a11y semantics)`.

### Task 3.5: Composer

**Files:** Create `web/src/components/session/Composer.tsx`; Test alongside.

**Interfaces:**
- Produces: `Composer({status, mode, onPrompt, onInterrupt, onSetMode, queued, onCancelQueued})` — auto-growing `<textarea aria-label="Message">` (grow to N lines then scroll); button shows **Send** when idle, **Queue** when working (dimmed + "Will send after current turn"), **Stop** available when working; "Plan mode" `role="switch"` toggle; a permission-mode chip opening a bottom sheet (interactive/auto/allowlist/full-auto, full-auto shows the banner); queued strip with per-item cancel.

- [ ] Steps: failing tests (idle→"Send", working→"Stop" + "Queue"; plan switch fires `onSetMode('plan')`; Enter submits, Shift+Enter newline; queued item cancel) → implement → pass → commit `feat(web): composer with queue/stop/plan-mode`.

### Task 3.6: SessionView assembly

**Files:** Create `web/src/components/session/{SessionView,TranscriptView,Turn,StatusBar,RotationDivider}.tsx`; Test `SessionView.test.tsx`.

**Interfaces:**
- Consumes: `useSessionSocket`, `transcriptReducer`, all block/card/composer components.
- Produces: `SessionView({sessionId, onBack})` — subscribes `sessionEvent` → `dispatch`; `Turn` is `React.memo` keyed by `turn.id`; `TranscriptView` scrolls the container (no `.focus()`/`scrollIntoView` on nodes) only when already at bottom (IntersectionObserver sentinel); on `replayComplete` with an open permission/question → force-scroll + sticky "Action needed" banner; aria-live mirror suppressed during replay (announce "history loaded" on `replayComplete`); `StatusBar role="status"` with human-readable text; empty-session + errored + all-accounts-limited states (latter shows reset time, calm copy). Replaces xterm.

- [ ] Steps: failing tests (events → rendered turns; `permissionRequest` shows the card + "Action needed" banner; status "working" shows Stop; all-limited error renders reset time) → implement → pass → commit `feat(web): SessionView rich transcript surface`.

### Task 3.7: Settings UI + New Session permission-mode select

**Files:** Create `web/src/components/settings/SettingsPanel.tsx`; Modify `web/src/api/hooks.ts`, `web/src/components/Dashboard.tsx`; Tests alongside.

**Interfaces:**
- Produces: `useSettings()`/`useUpdateSettings()` hooks; `SettingsPanel` (default permission mode incl. full-auto with banner, thinking-collapsed, repo root); New Session dialog gains a "Tool approvals" select pre-filled from settings; Dashboard routes a selected session to `SessionView` (replacing `Terminal`).

- [ ] Steps: failing tests (settings round-trip via mocked hooks; New Session sends `permission_mode`) → implement → pass → commit `feat(web): settings panel + per-session permission mode`.

---

## Phase 4 — Migration & cleanup

### Task 4.1: Delete xterm/PTY/base64; tidy

**Files:** Delete `web/src/components/Terminal.tsx`, `web/src/components/HelperKeyBar.tsx`, `server/sessions/manager.py`; Modify `web/package.json` (drop `@xterm/*`), `web/src/ws/client.ts` (drop `output` base64 in/out), `web/vitest.config.ts` (`clearMocks: true`); grep for stragglers.

- [ ] **Step 1:** `git rm web/src/components/Terminal.tsx web/src/components/HelperKeyBar.tsx server/sessions/manager.py`
- [ ] **Step 2:** Remove `@xterm/xterm` + `@xterm/addon-fit` from `web/package.json`; `npm install` to update the lockfile.
- [ ] **Step 3:** Remove the `output`/base64 input path from `client.ts` and any `manager`/`scrollback` references in `server/bridge/ws.py` and `orchestrator.py`. Run: `grep -rn "xterm\|scrollback\|HelperKeyBar\|base64\|SessionManager" web/src server | grep -v node_modules` → Expected: no live references (only this plan/spec docs).
- [ ] **Step 4:** Set `clearMocks: true` in `web/vitest.config.ts`.
- [ ] **Step 5: Run all suites** — `.venv/bin/python -m unittest discover -s tests`, `.venv/bin/python -m pytest server/tests/ -q`, and in `web/`: `npm run typecheck && npm run test && npm run build`. Expected: all green.
- [ ] **Step 6: Commit** `refactor: remove xterm/PTY/base64 terminal path (replaced by rich SessionView)`.

### Task 4.2: Full-suite gate + opt-in live smoke

**Files:** Create `docs/superpowers/SMOKE.md`.

- [ ] **Step 1:** Re-run all four suites; confirm green; record counts.
- [ ] **Step 2:** Write `SMOKE.md` — the manual, opt-in live checklist: start the service, open a session on a tiny repo, send "say hi", assert: a `turnStart`→`textDelta`→`turnEnd` renders, no `error`, the session auto-titles, and a permission card appears for a Bash prompt in interactive mode and gates execution until tapped.
- [ ] **Step 3: Commit** `docs: live smoke checklist for rich rendering`.

---

## Self-review

**Spec coverage:** interaction model (Phase 3) ✓; configurable+per-session permission default (2.3, 3.7) ✓; interrupt (1.5/3.5) ✓; slash commands (pass-through in composer 3.5) ✓; plan mode (1.5 `set_permission_mode`, 3.5 toggle) ✓; queue (2.4 server-side, 3.5 UI) ✓; AskUserQuestion buttons (0.3 gate, 1.3/3.4) ✓; auto-title (2.1 persists from translator/transcript `ai-title`; 1.6) — **note:** title extraction from `ai-title` is folded into Task 2.1's persistence; if the SDK doesn't surface it, fallback = first prompt (handle in 2.1). Security model (2.4/2.5/2.6) ✓; perf (memoized turns 3.6, highlight-on-settle 3.3, summary replay 1.6, batching 2.4) ✓; rotation (2.2) ✓; migration (4.1) ✓; spikes as gates (Phase 0) ✓.

**Placeholder scan:** Tasks 2.1/2.2/2.3/2.5/2.6 use prose "Steps:" summaries rather than full code blocks for the more mechanical backend wiring — acceptable because each names exact files, interfaces, and test assertions; the novel logic (events, translator, permissions, driver, replay, reducer) has complete code. No "TBD/TODO".

**Type consistency:** `ToolKind` values, `Translator.feed`, `PermissionRegistry.request/resolve/deny_all/is_granted`, `SessionDriver` method names, and the wire `type` strings are consistent across backend tasks and mirrored by `web/src/session/events.ts` (3.1) and the reducer (3.2).
