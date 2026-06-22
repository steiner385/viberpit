# Suggested Next Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each assistant turn goes idle, generate 2–4 short "next prompt" chips via a throwaway Max-account SDK query and show them above the composer; tapping a chip pre-fills the editable input.

**Architecture:** A new `SuggestionService` runs a one-shot, `resume`-less `ClaudeSDKClient` bound to the session's own account config dir (never the live conversation, never the paid API). The `SessionDriver` captures the last turn's text, and in its drain `finally` "idle" branch fires a fire-and-forget suggestion task that emits a new `Suggestions` event over the existing WebSocket batch channel. The frontend reducer stores the latest suggestions; the `Composer` renders them as chips and pre-fills its textarea on click.

**Tech Stack:** Python (asyncio, `claude_agent_sdk`, dataclasses) backend; React + TypeScript + Vite + Tailwind frontend; pytest (server), vitest + @testing-library/react (web).

## Global Constraints

- **Max subscription only — never the paid Anthropic API.** Generation reuses the session's account config dir.
- **Best-effort, non-blocking:** any suggestion failure (parse error, SDK error, rate-limit) emits nothing, logs, and must never block, delay, or fail a real turn.
- **Isolation (Principle II):** suggestion generation uses a *separate* `ClaudeSDKClient` with **no `resume`** — it must never touch the live session conversation.
- **Wire keys are camelCase** in `to_wire()` dicts (matches existing events).
- **This worktree has no built env.** Run commands exactly as written below.
  - **Server tests** — from the worktree root (`/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/68ae2804`):
    `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>`
  - **Web tests** — from `web/`: `./node_modules/.bin/vitest run <file>`, `./node_modules/.bin/tsc --noEmit`, `./node_modules/.bin/eslint <files>`. (`web/node_modules` is a symlink to the canonical checkout; do not delete it.)
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Branch: `feat/suggested-next-prompts` (already created, rebased onto `origin/master`).
- **Event dataclasses now carry a `parent_tool_use_id` field** (sub-agent threading). The new `Suggestions` event is session-level and does NOT need it; `isinstance(e, ev.TextDelta)` / `ev.TurnEnd` checks are unaffected.

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `server/sessions/events.py` | `Suggestions` event dataclass + `to_wire()` | Modify |
| `server/sessions/suggestions.py` | `SuggestionService`, `build_prompt`, `parse_suggestions`, `SUGGEST_SYSTEM_PROMPT` | Create |
| `server/sessions/sdk_factory.py` | `real_suggestion_factory` (Haiku, one-shot, tools denied) | Modify |
| `server/sessions/driver.py` | context capture, idle-branch trigger, cancellation, emit `Suggestions` | Modify |
| `server/sessions/orchestrator.py` | construct `SuggestionService`, inject into all 4 `SessionDriver` sites | Modify |
| `server/tests/test_suggestions.py` | unit tests for service + helpers | Create |
| `server/tests/test_driver_suggestions.py` | driver trigger/cancel/skip tests | Create |
| `web/src/session/events.ts` | `EvSuggestions`, union, `SESSION_EVENT_TYPES` | Modify |
| `web/src/session/reducer.ts` | `latestSuggestions` state + handlers | Modify |
| `web/src/session/reducer.test.ts` | reducer tests for suggestions | Modify |
| `web/src/components/session/Composer.tsx` | chip strip + pre-fill | Modify |
| `web/src/components/session/SessionView.tsx` | pass `suggestions` prop | Modify |
| `web/src/components/session/Composer.test.tsx` | chip render + pre-fill test | Create |

---

### Task 1: `Suggestions` backend event

**Files:**
- Modify: `server/sessions/events.py` (append at end of file, after the last dataclass on line ~117)
- Test: `server/tests/test_suggestions.py` (created here, extended in Tasks 2–3)

**Interfaces:**
- Produces: `ev.Suggestions(session_id: str, turn_id: str, items: list[str])` with
  `to_wire() -> {"type":"suggestions","sessionId":...,"turnId":...,"items":[...]}`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_suggestions.py`:

```python
from server.sessions.events import Suggestions


def test_suggestions_to_wire():
    ev = Suggestions(session_id="s1", turn_id="t1", items=["Run the tests", "Commit it"])
    assert ev.to_wire() == {
        "type": "suggestions",
        "sessionId": "s1",
        "turnId": "t1",
        "items": ["Run the tests", "Commit it"],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_suggestions_to_wire -v`
Expected: FAIL with `ImportError: cannot import name 'Suggestions'`

- [ ] **Step 3: Add the dataclass**

Append to `server/sessions/events.py` (the file already has `from dataclasses import dataclass` at the top):

```python
@dataclass
class Suggestions:
    session_id: str; turn_id: str; items: list[str]
    def to_wire(self): return {"type": "suggestions", "sessionId": self.session_id,
                               "turnId": self.turn_id, "items": self.items}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_suggestions_to_wire -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_suggestions.py
git commit -m "feat(events): add Suggestions session event

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Suggestion prompt-build + parse helpers

**Files:**
- Create: `server/sessions/suggestions.py`
- Test: `server/tests/test_suggestions.py`

**Interfaces:**
- Produces: `build_prompt(context: str) -> str`; `parse_suggestions(raw: str, limit: int = 4) -> list[str]`;
  module constant `SUGGEST_SYSTEM_PROMPT: str`. `parse_suggestions` tolerates surrounding prose/code
  fences, returns `[]` on any malformed input, filters non-string/empty items, truncates to `limit`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_suggestions.py`:

```python
from server.sessions.suggestions import build_prompt, parse_suggestions


def test_parse_plain_json_array():
    assert parse_suggestions('["Run tests", "Commit it"]') == ["Run tests", "Commit it"]


def test_parse_tolerates_surrounding_prose_and_fences():
    raw = 'Here you go:\n```json\n["A", "B"]\n```\nHope that helps!'
    assert parse_suggestions(raw) == ["A", "B"]


def test_parse_filters_nonstrings_and_blanks_and_truncates():
    raw = '["a", "", "  ", 5, null, "b", "c", "d", "e"]'
    assert parse_suggestions(raw, limit=4) == ["a", "b", "c", "d"]


def test_parse_garbage_returns_empty():
    assert parse_suggestions("not json at all") == []
    assert parse_suggestions("") == []
    assert parse_suggestions('{"not": "a list"}') == []


def test_build_prompt_includes_context():
    p = build_prompt("User: hi\n\nAssistant: hello")
    assert "User: hi" in p and "Assistant: hello" in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.suggestions'`

- [ ] **Step 3: Create the module with helpers**

Create `server/sessions/suggestions.py`:

```python
from __future__ import annotations
import json
import logging

log = logging.getLogger(__name__)

SUGGEST_SYSTEM_PROMPT = (
    "You suggest what the user might want to say NEXT in a live coding session with an "
    "AI agent. Reply with ONLY a JSON array of 2-4 short, imperative prompts (each under "
    '60 characters), no prose, no code fences. Example: ["Run the tests", "Commit it"]'
)


def build_prompt(context: str) -> str:
    return (
        "Here is the end of a transcript between a user and an AI coding agent:\n\n"
        f"{context}\n\n"
        "Suggest 2-4 short next prompts the user might send. Reply with a JSON array of "
        "strings only."
    )


def parse_suggestions(raw: str, limit: int = 4) -> list[str]:
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = [s.strip() for s in data if isinstance(s, str) and s.strip()]
    return out[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py -v`
Expected: PASS (all 6 tests including Task 1's)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/suggestions.py server/tests/test_suggestions.py
git commit -m "feat(suggestions): prompt-build and tolerant JSON parse helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `SuggestionService.generate()`

**Files:**
- Modify: `server/sessions/suggestions.py`
- Test: `server/tests/test_suggestions.py`

**Interfaces:**
- Consumes: `build_prompt`, `parse_suggestions` (Task 2); `Translator` (`server/sessions/translator.py`,
  which yields `ev.TextDelta` for streamed `text_delta` content); `events.TextDelta`. The
  `SDKClientProtocol` async-context-manager shape (`__aenter__`/`query`/`receive_response`/`__aexit__`).
- Produces: `SuggestionService(sdk_factory)` where `sdk_factory: (cwd: str, config_dir: str) -> SDKClientProtocol`;
  `async generate(cwd: str, config_dir: str, context: str) -> list[str]`. Returns `[]` on empty context
  or ANY exception (best-effort). Extracts assistant text by feeding stream messages through `Translator`
  and collecting `TextDelta.text`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_suggestions.py`:

```python
import asyncio
from server.sessions.suggestions import SuggestionService
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def test_generate_parses_streamed_json():
    msgs = [f.stream_start(uuid="t1"),
            f.stream_text('["Run the tests", "Commit it"]', uuid="t1"),
            f.result(uuid="t1")]
    svc = SuggestionService(lambda cwd, cfg: FakeSDKClient(msgs))
    out = asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "User: hi\n\nAssistant: done"))
    assert out == ["Run the tests", "Commit it"]


def test_generate_empty_context_skips():
    called = {"n": 0}

    def factory(cwd, cfg):
        called["n"] += 1
        return FakeSDKClient([])

    svc = SuggestionService(factory)
    assert asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "   ")) == []
    assert called["n"] == 0  # never built a client


def test_generate_swallows_errors():
    def boom(cwd, cfg):
        raise RuntimeError("sdk down")

    svc = SuggestionService(boom)
    assert asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "User: hi")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py -k generate -v`
Expected: FAIL with `ImportError: cannot import name 'SuggestionService'`

- [ ] **Step 3: Implement the service**

In `server/sessions/suggestions.py`, add these imports just below the existing `import logging`:

```python
from .translator import Translator
from . import events as ev
```

Then append the class at the bottom of the file:

```python
class SuggestionService:
    """Generates next-prompt suggestions via a throwaway, resume-less SDK query.

    `sdk_factory(cwd, config_dir) -> SDKClientProtocol` builds a one-shot client bound to the
    session's own account config dir. Best-effort: returns [] on empty context or any failure.
    """

    def __init__(self, sdk_factory):
        self._factory = sdk_factory

    async def generate(self, cwd: str, config_dir: str, context: str) -> list[str]:
        if not context.strip():
            return []
        try:
            client = self._factory(cwd, config_dir)
            tr = Translator()
            parts: list[str] = []
            async with client:
                await client.query(build_prompt(context))
                async for msg in client.receive_response():
                    for e in tr.feed(msg):
                        if isinstance(e, ev.TextDelta):
                            parts.append(e.text)
            return parse_suggestions("".join(parts))
        except Exception:
            log.warning("suggestion generation failed cwd=%s", cwd, exc_info=True)
            return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/suggestions.py server/tests/test_suggestions.py
git commit -m "feat(suggestions): SuggestionService.generate via throwaway SDK query

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Real Max-account suggestion factory

**Files:**
- Modify: `server/sessions/sdk_factory.py`
- Test: `server/tests/test_suggestions.py`

**Interfaces:**
- Consumes: `SUGGEST_SYSTEM_PROMPT` (Task 2).
- Produces: `real_suggestion_factory(cwd: str, config_dir: str) -> ClaudeSDKClient` — one-shot
  (`max_turns=1`), `resume=None`, tools denied, `CLAUDE_CONFIG_DIR=config_dir`, model from
  `CCSWITCH_SUGGEST_MODEL` env (default `"claude-haiku-4-5"`; empty string → account default).

Current full file (`server/sessions/sdk_factory.py`) — note it already does `import os` and imports
`COMPLETION_INSTRUCTION`; the production factory uses the `claude_code` preset, which the suggestion
factory must NOT use (we want a bare JSON generator):

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

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_suggestions.py`:

```python
def test_real_suggestion_factory_builds_isolated_oneshot(monkeypatch):
    import server.sessions.sdk_factory as sf

    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setenv("CCSWITCH_SUGGEST_MODEL", "claude-haiku-4-5")

    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct")
    opts = captured["options"]
    assert opts.cwd == "/tmp/r"
    assert opts.env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg-acct"
    assert opts.resume is None
    assert opts.max_turns == 1
    assert opts.model == "claude-haiku-4-5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_real_suggestion_factory_builds_isolated_oneshot -v`
Expected: FAIL with `AttributeError: module 'server.sessions.sdk_factory' has no attribute 'real_suggestion_factory'`

- [ ] **Step 3: Add the factory and deny-all callback**

Append to `server/sessions/sdk_factory.py`:

```python
async def _deny_all_tools(tool_name, tool_input, ctx=None):
    from claude_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="tools are disabled during suggestion generation")


def real_suggestion_factory(cwd, config_dir):
    """One-shot, resume-less client for generating next-prompt suggestions.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku but can be overridden (or cleared to the account default) via
    CCSWITCH_SUGGEST_MODEL.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .suggestions import SUGGEST_SYSTEM_PROMPT
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    model = os.environ.get("CCSWITCH_SUGGEST_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUGGEST_SYSTEM_PROMPT, model=model, resume=None,
    )
    return ClaudeSDKClient(options=opts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_real_suggestion_factory_builds_isolated_oneshot -v`
Expected: PASS

- [ ] **Step 5: ⚠️ Manual verification (Principle III) — Max + Haiku**

This cannot be unit-tested (needs a real account + quota). Pick a real account config dir (e.g. one under `~/.claude-accounts/cfg/...`) and run, from the worktree root:

```bash
PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -c "
import asyncio
from server.sessions.sdk_factory import real_suggestion_factory
from server.sessions.suggestions import SuggestionService
svc = SuggestionService(real_suggestion_factory)
print(asyncio.run(svc.generate('/tmp', '<a-real-account-CLAUDE_CONFIG_DIR>', 'User: add a test\n\nAssistant: done')))
"
```

Expected: a non-empty list of short strings. **If it errors or returns `[]` due to model rejection**, the Max plan does not allow selecting Haiku via the SDK — set `CCSWITCH_SUGGEST_MODEL=""` (account default model) in the deploy env and re-run; note the outcome in the PR description. (Either way the feature stays safe: `generate()` swallows the error and emits no chips.)

- [ ] **Step 6: Commit**

```bash
git add server/sessions/sdk_factory.py server/tests/test_suggestions.py
git commit -m "feat(suggestions): real Max-account one-shot suggestion factory

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Driver — context capture, idle-branch trigger, cancellation

**Files:**
- Modify: `server/sessions/driver.py`
- Create: `server/tests/test_driver_suggestions.py`

**Interfaces:**
- Consumes: `SuggestionService.generate` (Task 3); `events.Suggestions` (Task 1).
- Produces: `SessionDriver(..., suggester=None)` keyword arg. When `suggester` is set, after a turn
  goes idle (drain `finally`, empty queue, no pending permission/question) the driver emits exactly one
  `ev.Suggestions(session_id, last_turn_id, items)` if `items` is non-empty. A new prompt
  (`_start_turn`), `interrupt()`, or `aclose()` cancels any in-flight suggestion task. At most one task
  in flight per session.

Current relevant shapes (verbatim, with current line numbers):
- `__init__` signature ends at line ~34 (`on_status=None):`); instance attrs run through line 62
  (`self._last_turn_outcome: str = "success"`). Add new attrs after line 62.
- `_start_turn(self, text: str, images: list[dict] | None = None)` begins at line 254; its first body
  lines are the docstring then `images = images or []` (255-256), `self._turn_active = True` (257),
  `self._last_turn_outcome = "success"` (258).
- `_drain` begins at line 296. Its message loop (300-303) currently reads:
  ```python
          for e in self._tr.feed(msg):
              if isinstance(e, ev.TurnEnd):
                  self._last_turn_outcome = e.outcome
              await self._out.put(e)
  ```
  Its `finally` idle `else` branch (328-332) reads:
  ```python
          else:
              self._turn_active = False
              self._turns_completed += 1
              await self._put_status("idle", 0)
  ```
- `events()` is at line 347; `interrupt()` at 354 (clears `self._queue` then ...); `aclose()` at 363.
- `_dash_signals()` at line 189 returns a `StatusSignals` with `.pending_permission` and
  `.pending_question` bools.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_driver_suggestions.py`:

```python
import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import Suggestions
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


class _StubSuggester:
    def __init__(self, items, gate: asyncio.Event | None = None):
        self.items = items
        self.gate = gate
        self.calls = 0

    async def generate(self, cwd, config_dir, context):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.items


def test_emits_suggestions_after_idle():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester(["Run the tests", "Commit it"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
                if isinstance(e, Suggestions):
                    return
        await asyncio.wait_for(asyncio.create_task(consume()), timeout=1.0)
        await d.aclose()
        return got
    got = asyncio.run(run())
    sug_ev = next(e for e in got if isinstance(e, Suggestions))
    assert sug_ev.items == ["Run the tests", "Commit it"]
    assert sug_ev.session_id == "s1"


def test_empty_suggestions_emit_no_event():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester([])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        if d._suggest_task:
            await d._suggest_task
        await asyncio.sleep(0.05)
        await d.aclose()
        await t
        return got
    got = asyncio.run(run())
    assert sug.calls == 1
    assert not any(isinstance(e, Suggestions) for e in got)


def test_no_suggester_never_triggers():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me")
        await d.start()
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        await d.aclose()
        await t
        return d._suggest_task, got
    suggest_task, got = asyncio.run(run())
    assert suggest_task is None
    assert not any(isinstance(e, Suggestions) for e in got)


def test_new_prompt_cancels_inflight_suggestion():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    gate = asyncio.Event()  # never set → generate() blocks until cancelled
    sug = _StubSuggester(["late"], gate=gate)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await d.send_prompt("hi")
        await asyncio.sleep(0.1)            # idle → suggestion task launched, blocked on gate
        inflight = d._suggest_task
        await d.send_prompt("second")       # must cancel the in-flight suggestion task
        await asyncio.sleep(0.05)
        cancelled = inflight.cancelled()
        await d.aclose()
        await t
        return cancelled, got
    cancelled, got = asyncio.run(run())
    assert cancelled is True
    assert not any(isinstance(e, Suggestions) for e in got)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_suggestions.py -v`
Expected: FAIL — `SessionDriver.__init__() got an unexpected keyword argument 'suggester'`

- [ ] **Step 3: Add constructor param + state**

In `server/sessions/driver.py`, add `suggester=None` to the end of the `__init__` keyword args (the
signature line currently ending `on_status=None):` at line ~34):

```python
                 on_status=None, suggester=None):
```

Then add these lines immediately after `self._last_turn_outcome: str = "success"` (line 62):

```python
        self._suggester = suggester
        self._suggest_task: asyncio.Task | None = None
        self._last_user_text: str = ""
        self._last_assistant_parts: list[str] = []
        self._last_turn_id: str = ""
```

- [ ] **Step 4: Add cancel helper + suggestion runner**

In `server/sessions/driver.py`, add these methods immediately after `events()` (which ends at line ~352):

```python
    def _cancel_suggest(self) -> None:
        if self._suggest_task is not None and not self._suggest_task.done():
            self._suggest_task.cancel()

    def _maybe_start_suggestions(self) -> None:
        """Fire-and-forget suggestion generation once the session is idle. Best-effort."""
        if self._suggester is None:
            return
        sig = self._dash_signals()
        if sig.pending_permission or sig.pending_question:
            return
        context = f"User: {self._last_user_text}\n\nAssistant: {''.join(self._last_assistant_parts)}"
        context = context[-2000:]
        self._cancel_suggest()
        self._suggest_task = asyncio.create_task(self._run_suggestions(context, self._last_turn_id))

    async def _run_suggestions(self, context: str, turn_id: str) -> None:
        items = await self._suggester.generate(self.cwd, self.config_dir, context)
        if items:
            await self._out.put(ev.Suggestions(self.session_id, turn_id, items))
```

- [ ] **Step 5: Capture turn text in `_start_turn` and cancel stale suggestions**

In `_start_turn` (line 254), immediately after `images = images or []` (line 256) add:

```python
        self._cancel_suggest()
        self._last_user_text = text
        self._last_assistant_parts = []
```

- [ ] **Step 6: Accumulate assistant text + turn id in `_drain`**

In `_drain` (line 296), replace the message loop (lines 300-303):

```python
            for e in self._tr.feed(msg):
                if isinstance(e, ev.TurnEnd):
                    self._last_turn_outcome = e.outcome
                await self._out.put(e)
```

with:

```python
            for e in self._tr.feed(msg):
                if isinstance(e, ev.TextDelta):
                    self._last_assistant_parts.append(e.text)
                elif isinstance(e, ev.TurnEnd):
                    self._last_turn_outcome = e.outcome
                    self._last_turn_id = e.turn_id
                await self._out.put(e)
```

- [ ] **Step 7: Trigger in the idle branch of `_drain`**

In `_drain`'s `finally`, the `else` branch (lines 328-332), add `self._maybe_start_suggestions()` after
`await self._put_status("idle", 0)`:

```python
            else:
                self._turn_active = False
                self._turns_completed += 1
                await self._put_status("idle", 0)
                self._maybe_start_suggestions()
```

- [ ] **Step 8: Cancel on interrupt and aclose**

In `interrupt()` (line 354), add `self._cancel_suggest()` after `self._queue.clear()`.

In `aclose()` (line 363), add `self._cancel_suggest()` as the first line of the method body (before
`self._reg.deny_all(self.session_id)`).

- [ ] **Step 9: Run tests to verify they pass**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_suggestions.py server/tests/test_driver.py -v`
Expected: PASS (new suggestion tests + existing driver tests still green)

- [ ] **Step 10: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_suggestions.py
git commit -m "feat(driver): emit next-prompt Suggestions when a turn goes idle

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Orchestrator — construct and inject the SuggestionService

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_driver_suggestions.py` (orchestrator-level guard tests)

**Interfaces:**
- Consumes: `SuggestionService` (Task 3), `real_suggestion_factory` (Task 4), `SessionDriver(..., suggester=)` (Task 5).
- Produces: `SessionOrchestrator(..., suggestion_factory=None)`; `self._suggester` is a `SuggestionService`
  (or `None` when env `CCSWITCH_SUGGESTIONS=0`). All FOUR `SessionDriver(...)` construction sites pass
  `suggester=self._suggester`.

Current shapes: `__init__` signature spans lines 74-83 (params: `sdk_factory=None, work_dir=None,
poll_interval=60.0, usage_fn=None, repo_root=None, titling_interval=15.0`). `self._sdk_factory = sdk_factory
or real_sdk_factory` is at line 98. The import is line 38:
`from server.sessions.sdk_factory import real_sdk_factory`. There are **four** `SessionDriver(...)` call
sites: lines 245-252 (create_session), 611-625 (`_build` in ensure_driver), 701-709 (`_build` in
adopt_session), 955-963 (`_do_rotate`). Each ends with `on_status=self._persist_status,`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_driver_suggestions.py`:

```python
def test_orchestrator_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_SUGGESTIONS", "0")
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=_factory([]))
    assert orch._suggester is None


def test_orchestrator_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SUGGESTIONS", raising=False)
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=_factory([]))
    assert orch._suggester is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_suggestions.py -k orchestrator -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute '_suggester'`

- [ ] **Step 3: Add imports and constructor wiring**

In `server/sessions/orchestrator.py`, change the import on line 38 to:

```python
from server.sessions.sdk_factory import real_sdk_factory, real_suggestion_factory
from server.sessions.suggestions import SuggestionService
```

> `import os` is already present at the top of this file (used at line ~93). Do not duplicate it.

Add `suggestion_factory=None,` to the `__init__` signature (alongside `sdk_factory=None,` near line 76).

After `self._sdk_factory = sdk_factory or real_sdk_factory` (line 98) add:

```python
        if os.environ.get("CCSWITCH_SUGGESTIONS", "1") == "0":
            self._suggester = None
        else:
            self._suggester = SuggestionService(suggestion_factory or real_suggestion_factory)
```

- [ ] **Step 4: Pass `suggester` to all FOUR SessionDriver sites**

Add `suggester=self._suggester,` after the `on_status=self._persist_status,` line in each of the four
`SessionDriver(...)` calls (lines 245-252, 611-625, 701-709, 955-963). Example for the first site:

```python
        driver = SessionDriver(
            session_id, account, cwd, str(cfg_dir),
            mode=permission_mode, sdk_factory=self._sdk_factory,
            registry=self._registry, identity=identity,
            on_session_id=self._persist_claude_session_id,
            on_sync_back=functools.partial(ccswitch_accounts.sync_back, account),
            on_status=self._persist_status,
            suggester=self._suggester,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (full server suite green)

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_driver_suggestions.py
git commit -m "feat(orchestrator): wire SuggestionService into session drivers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Frontend event type + registry

**Files:**
- Modify: `web/src/session/events.ts`

**Interfaces:**
- Produces: `EvSuggestions { type: 'suggestions'; sessionId: string; turnId: string; items: string[] }`;
  added to the `SessionEvent` union and the `SESSION_EVENT_TYPES` set.

- [ ] **Step 1: Add the interface**

In `web/src/session/events.ts`, after `EvQueueUpdate` add:

```typescript
export interface EvSuggestions  { type: 'suggestions';  sessionId: string; turnId: string; items: string[] }
```

- [ ] **Step 2: Add to the union**

Add `| EvSuggestions` to the `SessionEvent` union:

```typescript
export type SessionEvent =
  | EvUser | EvTurnStart | EvTurnEnd | EvTextDelta | EvThinkingDelta
  | EvToolCall | EvToolResult | EvPermissionRequest | EvQuestion
  | EvSessionMeta | EvTitle | EvStatus | EvReplayComplete | EvError | EvQueueUpdate
  | EvSuggestions
```

- [ ] **Step 3: Add to SESSION_EVENT_TYPES**

Add `'suggestions'` to the set:

```typescript
export const SESSION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user','turnStart','turnEnd','textDelta','thinkingDelta','toolCall','toolResult',
  'permissionRequest','question','sessionMeta','title','status','replayComplete','error','queueUpdate',
  'suggestions',
])
```

- [ ] **Step 4: Typecheck**

Run (in `web/`): `./node_modules/.bin/tsc --noEmit`
Expected: PASS. If the reducer's `switch` is exhaustive and `tsc` errors on an unhandled `suggestions`
case, that is fixed in Task 8 — proceed and re-typecheck after Task 8.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/events.ts
git commit -m "feat(web): add suggestions session event type

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Reducer — `latestSuggestions` state

**Files:**
- Modify: `web/src/session/reducer.ts`
- Test: `web/src/session/reducer.test.ts`

**Interfaces:**
- Consumes: `EvSuggestions` (Task 7).
- Produces: `TranscriptState.latestSuggestions: string[]`; set by `suggestions` event; cleared on
  `turnStart` and on `user`. `initialTranscript.latestSuggestions = []`.

Current shapes: `TranscriptState` interface is at lines 53-65 (fields end with `subAgents: Record<string, SubThread>`);
`initialTranscript` at lines 67-79 (ends `subAgents: {},`). `transcriptReducer(state, ev: SessionEvent | LocalAction)`
at line 240. The `user` case is lines 259-291 (three `return` paths, each `{ ...state, ..., openBlockId: null }`).
The `turnStart` case is lines 293-307 (an idempotent `return state` / `return { ...state, openBlockId: null }`,
and an append path `return { ...state, turns: [...], openBlockId: null }`).

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/reducer.test.ts` (inside the existing `describe('transcriptReducer', …)` block,
using the existing `reduce` helper and `transcriptReducer`):

```typescript
  it('stores suggestions from a suggestions event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['Run tests', 'Commit'],
    })
    expect(s.latestSuggestions).toEqual(['Run tests', 'Commit'])
  })

  it('clears suggestions on the next turnStart', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['A'],
    })
    const s2 = transcriptReducer(s1, { type: 'turnStart', turnId: 't2' })
    expect(s2.latestSuggestions).toEqual([])
  })

  it('clears suggestions when the user sends a prompt', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'suggestions', sessionId: 's1', turnId: 't1', items: ['A'],
    })
    const s2 = transcriptReducer(s1, { type: 'user', turnId: 'u1', text: 'next thing' })
    expect(s2.latestSuggestions).toEqual([])
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/reducer.test.ts`
Expected: FAIL (`latestSuggestions` undefined / cases not handled)

- [ ] **Step 3: Add state field + initial value**

In `web/src/session/reducer.ts`, add to the `TranscriptState` interface (after `subAgents: Record<string, SubThread>` on line ~64):

```typescript
  latestSuggestions: string[]  // next-prompt chips for the current idle turn
```

Add to `initialTranscript` (after `subAgents: {},` on line ~78):

```typescript
  latestSuggestions: [],
```

- [ ] **Step 4: Handle the suggestions event and clear on turnStart/user**

Add a new case to the reducer `switch`:

```typescript
    case 'suggestions':
      return { ...state, latestSuggestions: ev.items }
```

In the `turnStart` case (lines 293-307), add `latestSuggestions: []` to the two object-returning paths
(the `return { ...state, openBlockId: null }` and the append `return { ...state, turns: [...], openBlockId: null }`).
Leave the bare idempotent `return state` path unchanged (suggestions were already cleared by the preceding
`user` event in that path).

In the `user` case (lines 259-291), add `latestSuggestions: []` to each of the three returned state objects.

- [ ] **Step 5: Run tests + typecheck**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/reducer.test.ts && ./node_modules/.bin/tsc --noEmit`
Expected: PASS, and `tsc` clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(web): reducer stores and clears next-prompt suggestions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Composer chips + pre-fill + SessionView wiring

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Modify: `web/src/components/session/SessionView.tsx`
- Create: `web/src/components/session/Composer.test.tsx`

**Interfaces:**
- Consumes: `TranscriptState.latestSuggestions` (Task 8).
- Produces: `Composer` gains optional prop `suggestions?: string[]`. Renders a chip per suggestion above
  the textarea (only when non-empty and not working). Clicking a chip calls `setText(s)`, focuses and
  auto-grows the textarea; it does NOT send. `SessionView` passes `suggestions={rt.state.latestSuggestions}`.

Current shapes: `Composer` `Props` interface is lines 9-22; `onPrompt: (text: string, images: {...}[]) => void`
(chips do NOT call this). `const [text, setText] = useState('')` at line 25. The textarea has
`aria-label="Message"` (line ~156) and uses `textareaRef`. `submit` is at lines 64-77. `isWorking` is derived
from `status.state` (used elsewhere in the file). In `SessionView.tsx` the `<Composer .../>` render is at
lines 163-175 (ends with `autoFocusKey={sessionId}`); `rt.state` is the reducer state.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/Composer.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Composer } from './Composer'

function setup(suggestions?: string[]) {
  const onPrompt = vi.fn()
  render(
    <Composer
      status={{ state: 'idle', count: 0 }}
      mode="default"
      onPrompt={onPrompt}
      onInterrupt={() => {}}
      onSetMode={() => {}}
      suggestions={suggestions}
    />,
  )
  return { onPrompt }
}

describe('Composer suggestions', () => {
  it('renders a chip per suggestion', () => {
    setup(['Run the tests', 'Commit it'])
    expect(screen.getByRole('button', { name: 'Run the tests' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Commit it' })).toBeInTheDocument()
  })

  it('pre-fills the textarea on chip click without sending', () => {
    const { onPrompt } = setup(['Run the tests'])
    fireEvent.click(screen.getByRole('button', { name: 'Run the tests' }))
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('renders no chips when suggestions is empty', () => {
    setup([])
    expect(screen.queryByTestId('suggestion-chips')).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/Composer.test.tsx`
Expected: FAIL (chips not rendered; `suggestions` prop unknown)

- [ ] **Step 3: Add the prop and chip strip**

In `web/src/components/session/Composer.tsx`:

Add `suggestions?: string[]` to the `Props` interface (after `autoFocusKey?: unknown` on line ~21):

```typescript
  suggestions?: string[]
```

Add `suggestions` to the destructured params of the component function (it begins
`export function Composer({ status, mode, onPrompt, ... }: Props) {`).

Add a `prefill` handler inside the component (near the `submit` callback, after line ~77):

```typescript
  const prefill = useCallback((value: string) => {
    setText(value)
    const el = textareaRef.current
    if (el) {
      el.focus()
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 192)}px`
    }
  }, [])
```

Render the chip strip near the top of the component's returned JSX, just before the textarea row, shown
only when there are suggestions and the session is not working (reuse the existing `isWorking` and `cn`
helper already imported in this file):

```tsx
      {suggestions && suggestions.length > 0 && !isWorking && (
        <div data-testid="suggestion-chips" className="flex flex-wrap gap-2">
          {suggestions.map((s, i) => (
            <button
              key={`${i}-${s.slice(0, 24)}`}
              type="button"
              aria-label={s}
              onClick={() => prefill(s)}
              className={cn(
                'rounded-full px-3 py-1 text-xs min-h-[44px]',
                'border border-blue-300 dark:border-blue-700',
                'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
                'hover:bg-blue-100 dark:hover:bg-blue-900/50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              {s.length > 60 ? s.slice(0, 60) + '…' : s}
            </button>
          ))}
        </div>
      )}
```

> If `isWorking` is not already defined in this component, derive it: `const isWorking = status.state === 'working'`.

- [ ] **Step 4: Pass the prop from SessionView**

In `web/src/components/session/SessionView.tsx`, add to the `<Composer ... />` render (after
`autoFocusKey={sessionId}` on line ~174):

```tsx
          suggestions={rt.state.latestSuggestions}
```

- [ ] **Step 5: Run tests + typecheck + lint**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/session/Composer.test.tsx && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/eslint src/components/session/Composer.tsx src/components/session/SessionView.tsx`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/SessionView.tsx web/src/components/session/Composer.test.tsx
git commit -m "feat(web): suggestion chips above composer with tap-to-prefill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Full verification + PR

**Files:** none (verification only)

- [ ] **Step 1: Server suite green**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=. /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/`
Expected: all pass.

- [ ] **Step 2: Web suite green**

Run (in `web/`): `./node_modules/.bin/vitest run && ./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/eslint .`
Expected: all pass / clean.

- [ ] **Step 3: Confirm the Principle III verification (Task 4 Step 5) outcome is recorded**

Ensure the PR description states whether `CCSWITCH_SUGGEST_MODEL` stays `claude-haiku-4-5` or was set to `""` (account default) based on the live Max check.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/suggested-next-prompts
gh pr create --title "feat: context-aware next-prompt suggestion chips" --body "$(cat <<'EOF'
Generates 2–4 tappable next-prompt chips after each turn goes idle, via a throwaway
Max-account SDK query (no paid API), pre-filling the editable composer on tap.

Spec: docs/superpowers/specs/2026-06-22-suggested-next-prompts-design.md
Plan: docs/superpowers/plans/2026-06-22-suggested-next-prompts.md

Principle III check (Max + Haiku model override): <record outcome here>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Generation via Max account, same account as session → Tasks 4, 6 (config_dir from the session's driver). ✔
- Automatic on every `TurnEnd` (idle) → Task 5 Step 7. ✔
- Throwaway, no-`resume` context → Task 4 (`resume=None`), Task 3 (separate client). ✔
- Cheapest model (Haiku) + verification + fallback → Task 4 Steps 3,5. ✔
- 2–4 short strings → Task 2 `parse_suggestions` (`limit=4`). ✔
- Pre-fill editable (not send) → Task 9 Step 3 (`prefill`, `onPrompt` untouched). ✔
- `Suggestions` event + `to_wire` + `SESSION_EVENT_TYPES` + reducer + cleared on prompt/turnStart → Tasks 1, 7, 8. ✔
- Best-effort never blocks → Task 3 (`except` → `[]`), Task 5 (`_run_suggestions` fire-and-forget). ✔
- Stale cancellation → Task 5 Steps 4,5,8. ✔
- Skip when permission/question pending → Task 5 Step 4 (`_maybe_start_suggestions` guard). ✔
- One in flight per session → Task 5 (`_cancel_suggest` before new task). ✔
- Tests for service/driver/reducer/UI → Tasks 2,3,5,8,9. ✔

**Placeholder scan:** No TBD/TODO; every code step shows full code; the only manual step (Task 4 Step 5) is an unavoidable live-account check with an exact command and documented fallback.

**Type consistency:** `Suggestions(session_id, turn_id, items)` ↔ wire `{sessionId,turnId,items}` ↔ TS `EvSuggestions{sessionId,turnId,items}` ↔ reducer `latestSuggestions: string[]` ↔ Composer `suggestions?: string[]`. `SuggestionService(sdk_factory)` / `generate(cwd, config_dir, context)` consistent across Tasks 3, 4, 6. `_cancel_suggest`/`_maybe_start_suggestions`/`_run_suggestions`/`_suggest_task` names consistent across Task 5. ✔
