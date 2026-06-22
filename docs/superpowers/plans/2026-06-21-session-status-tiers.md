# Session Status Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sessions list distinguish a clean idle turn-end (**Done**) from an agent-signalled objective completion (**Complete**) and from an errored / limit-hit / stream-died turn (**Needs attention**).

**Architecture:** The SDK's per-turn outcome (currently discarded) is captured in the Translator and stamped onto the `TurnEnd` event; the SessionDriver records the last turn's outcome (and forces `failed` on a stream exception); `derive_status` maps that outcome to one of three terminal states. A collision-proof completion marker (`<!-- ccswitch:complete -->`) is auto-appended to every session's Claude Code system prompt so the agent can signal completion with no user action. The frontend gains the two new badge states and recolors Done to neutral.

**Tech Stack:** Python 3.12 + `claude-agent-sdk` 0.2.105 (backend, pytest); React + TypeScript + Vitest (frontend); `lucide-react` icons; Tailwind.

## Global Constraints

- Completion marker literal (verbatim, single source of truth): `<!-- ccswitch:complete -->`
- Outcome-based detection ONLY — no time-based stall detection, no completion-language NLP.
- A turn is `failed` when `ResultMessage.subtype != "success"` (covers `error_max_turns`, `error_during_execution`) OR the drain stream raised.
- `ev.TurnEnd.outcome` defaults to `"success"` (backward-compatible with replay / existing constructors).
- Existing status precedence is unchanged: pending plan → `ready_review`; pending permission/question → `waiting_input`; active turn → `in_progress`. The new states only refine the post-turn terminal branch.
- `interrupt()` (user Stop) is NOT "needs attention" — leave its behavior (emits `idle`, does not increment `turns_completed`) untouched.
- Backend tests run from the repo root with the project venv: `python -m pytest <path> -v`. Frontend from `web/`: `npm run test -- <pattern>` and `npm run build`.

---

### Task 1: Completion constants module

**Files:**
- Create: `server/sessions/completion.py`
- Test: `server/tests/test_completion.py`

**Interfaces:**
- Produces: `COMPLETION_MARKER: str` (the literal `<!-- ccswitch:complete -->`) and `COMPLETION_INSTRUCTION: str` (the system-prompt append text, which must contain `COMPLETION_MARKER`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_completion.py
from server.sessions.completion import COMPLETION_MARKER, COMPLETION_INSTRUCTION


def test_marker_is_html_comment():
    assert COMPLETION_MARKER == "<!-- ccswitch:complete -->"


def test_instruction_embeds_the_marker_verbatim():
    # The injected instruction and the detector must reference the same literal.
    assert COMPLETION_MARKER in COMPLETION_INSTRUCTION
    # It should tell the agent to emit it only when genuinely done.
    assert "only" in COMPLETION_INSTRUCTION.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_completion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.completion'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/completion.py
"""Single source of truth for the session-completion marker.

The same literal is appended to every session's system prompt (so the agent
knows how to signal completion) and scanned for by the Translator (so the
dashboard can show 'Complete'). Keeping both in one module prevents drift.
"""
from __future__ import annotations

COMPLETION_MARKER = "<!-- ccswitch:complete -->"

COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    "while you are still working or waiting on something."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_completion.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/completion.py server/tests/test_completion.py
git commit -m "feat(sessions): completion marker + instruction constants"
```

---

### Task 2: Translator stamps the turn outcome onto TurnEnd

**Files:**
- Modify: `server/sessions/events.py:29-32` (add `outcome` to `TurnEnd`)
- Modify: `server/sessions/translator.py` (accumulate assistant text; classify outcome on `ResultMessage`)
- Modify: `server/tests/fakes/fake_sdk.py:38-42` (let `result()` set `subtype`/`is_error`)
- Test: `server/tests/test_translator.py` (add outcome cases)

**Interfaces:**
- Consumes: `COMPLETION_MARKER` from Task 1.
- Produces: `ev.TurnEnd` now has `outcome: str = "success"` (one of `"success"`, `"complete"`, `"failed"`). `fake_sdk.result(..., subtype=..., is_error=...)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_translator.py
from claude_agent_sdk import TextBlock
from server.sessions.translator import Translator
from server.sessions.completion import COMPLETION_MARKER
from server.tests.fakes import fake_sdk as f


def _drive(msgs):
    tr = Translator()
    out = []
    for m in msgs:
        out.extend(tr.feed(m))
    return out


def _turn_end(events):
    from server.sessions.events import TurnEnd
    return next(e for e in events if isinstance(e, TurnEnd))


def test_turn_end_outcome_success_without_marker():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="all set"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "success"


def test_turn_end_outcome_complete_with_marker():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"done\n{COMPLETION_MARKER}"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "complete"


def test_turn_end_outcome_failed_on_error_subtype():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="boom"), uuid="t1"),
            f.result(session_id="s", uuid="t1", subtype="error_during_execution", is_error=True)]
    assert _turn_end(_drive(msgs)).outcome == "failed"


def test_marker_on_an_errored_turn_is_still_failed():
    # subtype precedence: an errored turn is never 'complete' even if text has the marker.
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"{COMPLETION_MARKER}"), uuid="t1"),
            f.result(session_id="s", uuid="t1", subtype="error_max_turns", is_error=True)]
    assert _turn_end(_drive(msgs)).outcome == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_translator.py -k outcome -v`
Expected: FAIL — `result() got an unexpected keyword argument 'subtype'` (and `outcome` attribute missing).

- [ ] **Step 3a: Add the `outcome` field to `TurnEnd`**

In `server/sessions/events.py`, replace the `TurnEnd` dataclass body:

```python
@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; outcome: str = "success"
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "outcome": self.outcome}
```

- [ ] **Step 3b: Let the fake `result()` set subtype/is_error**

In `server/tests/fakes/fake_sdk.py`, replace the `result` helper:

```python
def result(session_id: str = "sess-1", usage: dict | None = None, uuid: str = "u9",
           cost: float | None = None, subtype: str = "success", is_error: bool = False) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id=session_id, usage=usage, uuid=uuid,
                         total_cost_usd=cost)
```

- [ ] **Step 3c: Accumulate assistant text and classify on ResultMessage**

In `server/sessions/translator.py`:

1. Add `TextBlock` to the SDK import:
```python
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, TextBlock)
from .completion import COMPLETION_MARKER
```

2. In `__init__`, initialize the buffer (next to `self._seen_tools`):
```python
        self._turn_text: str = ""
```

3. In `_reset`, clear it:
```python
    def _reset(self):
        self._started = False
        self._turn_id = ""
        self._seen_tools = set()
        self._turn_text = ""
```

4. In the `AssistantMessage` branch, accumulate text-block text (add inside the existing `for b in msg.content:` loop, before/after the `ToolUseBlock` check):
```python
            for b in msg.content:
                if isinstance(b, TextBlock):
                    self._turn_text += b.text
                if isinstance(b, ToolUseBlock) and b.id not in self._seen_tools:
                    self._seen_tools.add(b.id)
                    if b.name == "AskUserQuestion":
                        continue
                    out.append(ev.ToolCall(self._turn_id, b.id, b.name,
                                           ev.tool_kind(b.name), b.input, getattr(msg, "uuid", None)))
            return out
```

5. In the `ResultMessage` branch, classify the outcome (capture text BEFORE `_reset()` clears it):
```python
        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_id or "turn"
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif COMPLETION_MARKER in self._turn_text:
                outcome = "complete"
            else:
                outcome = "success"
            self._reset()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                               outcome)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_translator.py -v`
Expected: PASS (all existing translator tests + the 4 new outcome tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/fakes/fake_sdk.py server/tests/test_translator.py
git commit -m "feat(sessions): classify turn outcome (success/complete/failed) on TurnEnd"
```

---

### Task 3: derive_status maps outcome to the terminal state

**Files:**
- Modify: `server/sessions/status.py` (add `last_turn_outcome` to `StatusSignals`; new terminal branch)
- Test: `server/tests/test_derive_status.py`

**Interfaces:**
- Consumes: nothing new (string outcome).
- Produces: `StatusSignals(..., last_turn_outcome: str = "success")`; `derive_status` returns `"complete"` / `"needs_attention"` in addition to the existing states.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_derive_status.py`, add `last_turn_outcome` to the `S` helper base dict and add parametrize cases:

```python
def S(**kw):
    base = dict(turn_active=False, pending_permission=False,
                pending_question=False, pending_plan=False, turns_completed=0,
                last_turn_outcome="success")
    base.update(kw)
    return StatusSignals(**base)
```

Add these cases to the `@pytest.mark.parametrize` list:

```python
    (S(turns_completed=1, last_turn_outcome="success"),               "done"),
    (S(turns_completed=1, last_turn_outcome="complete"),              "complete"),
    (S(turns_completed=1, last_turn_outcome="failed"),                "needs_attention"),
    # an active turn still outranks any terminal outcome
    (S(turn_active=True, turns_completed=1, last_turn_outcome="failed"),   "in_progress"),
    # a pending prompt still outranks a 'complete' outcome
    (S(turns_completed=1, last_turn_outcome="complete", pending_question=True), "waiting_input"),
    # outcome is ignored before the first completed turn
    (S(turns_completed=0, last_turn_outcome="complete"),             "idle"),
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_derive_status.py -v`
Expected: FAIL — `StatusSignals.__init__() got an unexpected keyword argument 'last_turn_outcome'`.

- [ ] **Step 3: Update `status.py`**

```python
@dataclass(frozen=True)
class StatusSignals:
    turn_active: bool
    pending_permission: bool
    pending_question: bool
    pending_plan: bool
    turns_completed: int
    last_turn_outcome: str = "success"


def derive_status(sig: StatusSignals) -> str:
    if sig.pending_plan:
        return "ready_review"
    if sig.pending_permission or sig.pending_question:
        return "waiting_input"
    if sig.turn_active:
        return "in_progress"
    if sig.turns_completed >= 1:
        if sig.last_turn_outcome == "failed":
            return "needs_attention"
        if sig.last_turn_outcome == "complete":
            return "complete"
        return "done"
    return "idle"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_derive_status.py -v`
Expected: PASS (all original + 6 new cases).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/status.py server/tests/test_derive_status.py
git commit -m "feat(sessions): derive complete/needs_attention from last-turn outcome"
```

---

### Task 4: Driver records the last-turn outcome (incl. stream-failure)

**Files:**
- Modify: `server/sessions/driver.py` (init field; read outcome in `_drain`; force `failed` in `except`; pass through `_dash_signals`)
- Test: `server/tests/test_driver_status.py`

**Interfaces:**
- Consumes: `ev.TurnEnd.outcome` (Task 2); `StatusSignals.last_turn_outcome` (Task 3).
- Produces: terminal dashboard status of `complete` / `done` / `needs_attention` via the existing `on_status` callback.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_driver_status.py` (reuse its existing `_factory`, `on_status` capture pattern):

```python
from claude_agent_sdk import TextBlock as _TB
from server.sessions.completion import COMPLETION_MARKER as _MARK


def _run_capture(msgs):
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    return asyncio.run(run())


def test_status_complete_when_marker_present():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"),
            f.assistant(_TB(text=f"done {_MARK}"), uuid="t1"),
            f.result(session_id="s1", uuid="t1")]
    assert _run_capture(msgs)[-1] == "complete"


def test_status_needs_attention_on_error_result():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"),
            f.assistant(_TB(text="boom"), uuid="t1"),
            f.result(session_id="s1", uuid="t1", subtype="error_during_execution", is_error=True)]
    assert _run_capture(msgs)[-1] == "needs_attention"
```

Then a stream-failure test (the drain `except` path). Add a tiny raising client at the top of the file:

```python
class _RaisingClient(FakeSDKClient):
    async def receive_response(self):
        raise RuntimeError("stream died")


def test_status_needs_attention_when_stream_raises():
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        return _RaisingClient([f.sys_init("s1")], on_can_use_tool=can_use_tool)
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    assert asyncio.run(run())[-1] == "needs_attention"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_driver_status.py -k "complete or needs_attention" -v`
Expected: FAIL — terminal status is `done`/`idle` (outcome not yet tracked).

- [ ] **Step 3: Update `driver.py`**

1. In `__init__`, next to `self._turns_completed = 0`:
```python
        self._last_turn_outcome: str = "success"
```

2. In `_drain`, capture the outcome as events stream by. Replace the inner loop body so the `TurnEnd` outcome is recorded:
```python
            async for msg in self._client.receive_response():
                for e in self._tr.feed(msg):
                    if isinstance(e, ev.TurnEnd):
                        self._last_turn_outcome = e.outcome
                    await self._out.put(e)
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
                    if self._on_session_id is not None:
                        try:
                            await self._on_session_id(self.session_id, self.claude_session_id)
                        except Exception:
                            log.exception("driver.on_session_id session=%s", self.session_id)
```
(`ev` is already imported in driver.py; confirm with `grep "import.*events as ev\|from . import events" server/sessions/driver.py` — if it imports `TurnEnd` by name instead, use that name.)

3. In the `except Exception:` block of `_drain`, force the failed outcome (add after the existing `await self._out.put(ev.ErrorEvent(...))`):
```python
        except Exception:
            log.exception("driver.drain session=%s", self.session_id)
            await self._out.put(ev.ErrorEvent("driver_error", "session stream failed"))
            self._last_turn_outcome = "failed"
```

4. In `_dash_signals`, pass the outcome through:
```python
    def _dash_signals(self) -> StatusSignals:
        perms = self._reg.pending(self.session_id)
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(perms),
            pending_question=bool(self._qreg.pending(self.session_id)),
            pending_plan=any(p.get("tool") == "ExitPlanMode" for p in perms),
            turns_completed=self._turns_completed,
            last_turn_outcome=self._last_turn_outcome,
        )
```

5. Reset the outcome when a new turn starts so a prior `failed`/`complete` doesn't leak into the next turn's in-progress state. In `_start_turn`, right after `self._turn_active = True`:
```python
        self._turn_active = True
        self._last_turn_outcome = "success"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_driver_status.py -v`
Expected: PASS (existing `done` progression test + 3 new).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_status.py
git commit -m "feat(sessions): driver tracks last-turn outcome incl. stream-failure"
```

---

### Task 5: Auto-inject the completion instruction into the session system prompt

**Files:**
- Modify: `server/sessions/sdk_factory.py`
- Test: `server/tests/test_sdk_factory.py`

**Interfaces:**
- Consumes: `COMPLETION_INSTRUCTION` (Task 1).
- Produces: every real session client built with `system_prompt={"type":"preset","preset":"claude_code","append": COMPLETION_INSTRUCTION}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sdk_factory.py
from server.sessions.completion import COMPLETION_MARKER


def _captured_options(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)

    from server.sessions.sdk_factory import real_sdk_factory
    real_sdk_factory("/tmp/cwd", "/tmp/cfg", lambda *a, **k: None, "interactive", None)
    return captured["options"]


def test_factory_appends_completion_instruction(monkeypatch):
    opts = _captured_options(monkeypatch)
    sp = opts.system_prompt
    assert isinstance(sp, dict)
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert COMPLETION_MARKER in sp["append"]


def test_factory_still_sets_core_options(monkeypatch):
    opts = _captured_options(monkeypatch)
    assert opts.cwd == "/tmp/cwd"
    assert opts.permission_mode == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_sdk_factory.py -v`
Expected: FAIL — `system_prompt` is `None` (`assert isinstance(sp, dict)`).

- [ ] **Step 3: Update `sdk_factory.py`**

```python
"""Production factory: builds a real ClaudeSDKClient for a SessionDriver."""
from __future__ import annotations
import os

from .completion import COMPLETION_INSTRUCTION


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": COMPLETION_INSTRUCTION,
        },
    )
    return ClaudeSDKClient(options=opts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_sdk_factory.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/sdk_factory.py server/tests/test_sdk_factory.py
git commit -m "feat(sessions): auto-append completion-marker instruction to session prompt"
```

---

### Task 6: Frontend — add Complete & Needs attention states; recolor Done

**Files:**
- Modify: `web/src/api/types.ts:24-26` (the `SessionStatus` union)
- Modify: `web/src/components/UsageBadge.tsx` (icon imports; `SESSION_STATUS_MAP`)
- Test: `web/src/components/__tests__/UsageBadge.test.tsx`

**Interfaces:**
- Consumes: backend now emits `status` of `"complete"` / `"needs_attention"`.
- Produces: rendered badges for the new states; `done` rendered neutral.

- [ ] **Step 1: Write the failing test**

Append to `web/src/components/__tests__/UsageBadge.test.tsx` (match the file's existing render/query style — it renders `StatusPill` with a `status` prop):

```tsx
import { render, screen } from '@testing-library/react'
import { StatusPill } from '../UsageBadge'

describe('StatusPill — session status tiers', () => {
  it('renders Complete with emerald text', () => {
    const { container } = render(<StatusPill status="complete" />)
    expect(screen.getByText('Complete')).toBeInTheDocument()
    expect(container.querySelector('.text-emerald-700')).toBeTruthy()
  })

  it('renders Needs attention with amber text', () => {
    const { container } = render(<StatusPill status="needs_attention" />)
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
    expect(container.querySelector('.text-amber-700')).toBeTruthy()
  })

  it('renders Done with neutral (non-emerald) text', () => {
    const { container } = render(<StatusPill status="done" />)
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(container.querySelector('.text-emerald-700')).toBeFalsy()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npm run test -- UsageBadge`
Expected: FAIL — "Needs attention" not found; Done still emerald.

- [ ] **Step 3a: Extend the `SessionStatus` union**

In `web/src/api/types.ts`, add the two members to the union (it currently ends `… | 'limited' | 'ended'`):

```typescript
export type SessionStatus =
  | 'idle'
  | 'in_progress'
  | 'waiting_input'
  | 'ready_review'
  | 'done'
  | 'complete'
  | 'needs_attention'
  | 'limited'
  | 'ended'
```

(Keep whatever members already exist; only add `'complete'` and `'needs_attention'`.)

- [ ] **Step 3b: Update `SESSION_STATUS_MAP` and icons**

In `web/src/components/UsageBadge.tsx`, ensure `Check` is imported from `lucide-react` (alongside the existing `CheckCircle2`, `AlertTriangle`, etc.):

```typescript
import { /* …existing… */ Check } from 'lucide-react'
```

Recolor `done` and add the two new entries (replace the existing `done` block):

```typescript
  done: {
    label: 'Done',
    Icon: Check,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
  complete: {
    label: 'Complete',
    Icon: CheckCircle2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  needs_attention: {
    label: 'Needs attention',
    Icon: AlertTriangle,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
```

- [ ] **Step 4: Run test + build to verify**

Run (from `web/`): `npm run test -- UsageBadge`
Expected: PASS.
Run (from `web/`): `npm run build`
Expected: `tsc -b && vite build` succeeds (the union exhaustiveness compiles).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/components/UsageBadge.tsx web/src/components/__tests__/UsageBadge.test.tsx
git commit -m "feat(web): Complete & Needs attention status badges; recolor Done neutral"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests -q`
Expected: all pass (this is the exact gate `auto-deploy.sh` runs).

- [ ] **Step 2: Frontend build + tests**

Run (from `web/`): `npm run test && npm run build`
Expected: tests pass; build green.

- [ ] **Step 3: Commit any incidental fixes** (only if Steps 1-2 surfaced something)

```bash
git add -A && git commit -m "test: fixups for session status tiers"
```

---

## Self-Review

**Spec coverage:**
- Three terminal states (Complete/Done/Needs attention) → Tasks 3 (mapping), 6 (UI). ✓
- Translator owns SDK→outcome → Task 2. ✓
- Driver tracks outcome incl. except path → Task 4. ✓
- `derive_status` precedence unchanged → Task 3 tests assert pending-*/active outrank. ✓
- Auto-injected marker instruction (no user action) → Task 5. ✓
- Shared marker constant (no drift) → Task 1; consumed by Tasks 2 & 5; guarded by Task 5 test. ✓
- Frontend states + Done recolor → Task 6. ✓
- Tests per component → every task is TDD. ✓
- `TurnEnd.outcome` default `"success"` (backward compat) → Task 2 Step 3a. ✓
- `interrupt()` untouched → not modified by any task (Task 4 only touches `_start_turn`/`_drain`/`_dash_signals`/`__init__`). ✓

**Placeholder scan:** none — all steps carry concrete code and exact run commands.

**Type consistency:** `outcome` is a `str` everywhere (`"success"`/`"complete"`/`"failed"`); the UI states are `"complete"`/`"needs_attention"`; `last_turn_outcome` name matches across `status.py`, the driver `_dash_signals`, and the `S(...)` test helper. `TurnEnd(turn_id, usage, cost_usd, src_uuid, outcome)` positional order matches the translator's emit call in Task 2 Step 3c.
