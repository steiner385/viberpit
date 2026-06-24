# Context-Condensing Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a "Condensing context…" status (spinner + elapsed) while a `claude` session auto-compacts, and make the existing context-% readout escalate color as the window fills.

**Architecture:** The condensing state is a transient in-memory driver flag, fed by the SDK `PreCompact` hook (start) and the `compact_boundary` stream message (end). It rides the existing status pipeline (`derive_status` → `Status` event → status pill / StatusBar). The context meter is the *existing* `remainingContextPct` → `SessionMeta` readout, enhanced frontend-only with a severity → color mapping. No determinate ETA (the SDK exposes no compaction progress).

**Tech Stack:** Python (FastAPI/asyncio, pytest), `claude-agent-sdk` 0.2.105, React+TS (vitest), Tailwind, lucide-react.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-condensing-indicator-design.md`.
- New model columns must be nullable — **but this feature persists nothing new**; `_condensing` is in-memory only and must never be written to the DB `status` column.
- Backend lint gates (run before pushing): `ruff`, `mypy`, `pytest server/tests/`. mypy skips untyped fn bodies.
- Web gates: `tsc -b` (the REAL typecheck — `tsc --noEmit` checks nothing), `vitest run`, `eslint` (max-warnings 0; no ref read during render).
- Run backend tests against the prebuilt env with the worktree on the path:
  `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest …` — see CLAUDE.md "Dev / testing".
- Internal events (no `to_wire`) are consumed by the driver and never forwarded to the UI; the new `condensing` status is the only UI-visible signal for piece 1.
- `condensing` status string must outrank `turn_active` (compaction happens mid-turn) but rank below the needs-you states (`ready_review`/`waiting_question`/`waiting_input`).

---

## Task 1: `condensing` status in the pure derivation

**Files:**
- Modify: `server/sessions/status.py`
- Test: `server/tests/test_status.py` (existing table tests for `derive_status`)

**Interfaces:**
- Produces: `StatusSignals(..., condensing: bool = False)`; `derive_status` returns `"condensing"` when `condensing` is set and no needs-you state is pending.

- [ ] **Step 1: Write the failing test.** Append to `server/tests/test_status.py`:

```python
from server.sessions.status import StatusSignals, derive_status


def _sig(**kw):
    base = dict(turn_active=False, pending_permission=False, pending_question=False,
                pending_plan=False, turns_completed=0)
    base.update(kw)
    return StatusSignals(**base)


def test_condensing_outranks_active_turn():
    assert derive_status(_sig(turn_active=True, condensing=True)) == "condensing"


def test_condensing_yields_only_when_set():
    assert derive_status(_sig(turn_active=True, condensing=False)) == "in_progress"


def test_needs_you_states_outrank_condensing():
    assert derive_status(_sig(condensing=True, pending_permission=True)) == "waiting_input"
    assert derive_status(_sig(condensing=True, pending_question=True)) == "waiting_question"
    assert derive_status(_sig(condensing=True, pending_plan=True)) == "ready_review"
```

- [ ] **Step 2: Run the tests, verify they fail.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_status.py -q`
Expected: FAIL — `StatusSignals.__init__() got an unexpected keyword argument 'condensing'`.

- [ ] **Step 3: Implement.** In `server/sessions/status.py`, add the field to the dataclass (after `pending_plan`) and the rung to `derive_status`:

```python
@dataclass(frozen=True)
class StatusSignals:
    turn_active: bool
    pending_permission: bool
    pending_question: bool
    pending_plan: bool
    turns_completed: int
    last_turn_outcome: str = "success"
    condensing: bool = False
```

```python
def derive_status(sig: StatusSignals) -> str:
    if sig.pending_plan:
        return "ready_review"
    if sig.pending_question:
        return "waiting_question"
    if sig.pending_permission:
        return "waiting_input"
    if sig.condensing:
        return "condensing"
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

Also update the module docstring's precedence sentence to mention condensing between permission and active turn.

- [ ] **Step 4: Run the tests, verify they pass (and the existing ones still pass).**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_status.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add server/sessions/status.py server/tests/test_status.py
git commit -m "feat(status): add condensing status to derive_status"
```

---

## Task 2: `Condensing` event + translator `compact_boundary` handling

**Files:**
- Modify: `server/sessions/events.py`, `server/sessions/translator.py`, `server/tests/fakes/fake_sdk.py`
- Test: `server/tests/test_translator.py` (existing)

**Interfaces:**
- Consumes: real SDK `SystemMessage(subtype="compact_boundary", data={"compact_metadata": {...}})`.
- Produces: `ev.Condensing(active: bool, trigger: str | None = None, pre_tokens: int | None = None)` (internal, no `to_wire`). Fake builder `sys_compact_boundary(trigger="auto", pre_tokens=None)`.

- [ ] **Step 1: Write the failing test.** Add to `server/tests/test_translator.py` (import `events as ev`, `fakes.fake_sdk as f`, and the `Translator`):

```python
def test_compact_boundary_emits_condensing_inactive():
    tr = Translator()
    out = tr.feed(f.sys_compact_boundary(trigger="auto", pre_tokens=1234))
    assert len(out) == 1
    assert isinstance(out[0], ev.Condensing)
    assert out[0].active is False
    assert out[0].trigger == "auto"
    assert out[0].pre_tokens == 1234


def test_non_init_non_boundary_system_message_is_ignored():
    tr = Translator()
    from claude_agent_sdk import SystemMessage
    assert tr.feed(SystemMessage(subtype="something_else", data={})) == []
```

- [ ] **Step 2: Run the tests, verify they fail.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_translator.py -q`
Expected: FAIL — `module 'server.tests.fakes.fake_sdk' has no attribute 'sys_compact_boundary'` (and `ev.Condensing` undefined).

- [ ] **Step 3: Implement the event.** In `server/sessions/events.py`, after the `RateLimit` dataclass, add:

```python
@dataclass
class Condensing:
    """Internal: context auto-compaction in progress. The driver toggles
    ``self._condensing`` off this event; the UI learns via the derived
    ``condensing`` status, so there is no wire form."""
    active: bool
    trigger: str | None = None
    pre_tokens: int | None = None
```

- [ ] **Step 4: Implement the translator branch.** In `server/sessions/translator.py`, replace the `SystemMessage` block so the final `return []` is preceded by a `compact_boundary` check:

```python
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model:
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
            if msg.subtype == "compact_boundary":
                meta = (msg.data or {}).get("compact_metadata") or {}
                return [ev.Condensing(active=False, trigger=meta.get("trigger"),
                                      pre_tokens=meta.get("pre_tokens"))]
            return []
```

- [ ] **Step 5: Implement the fake builder.** In `server/tests/fakes/fake_sdk.py`, after `sys_init`, add:

```python
def sys_compact_boundary(trigger: str = "auto", pre_tokens: int | None = None) -> SystemMessage:
    return SystemMessage(subtype="compact_boundary",
                         data={"compact_metadata": {"trigger": trigger, "pre_tokens": pre_tokens}})
```

- [ ] **Step 6: Run the tests, verify they pass.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_translator.py -q`
Expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/fakes/fake_sdk.py server/tests/test_translator.py
git commit -m "feat(translator): emit internal Condensing event on compact_boundary"
```

---

## Task 3: Driver flag, PreCompact callback, drain clearing

**Files:**
- Modify: `server/sessions/driver.py`
- Test: `server/tests/test_driver_status.py` (existing; uses inline `def factory(...)`)

**Interfaces:**
- Consumes: `StatusSignals.condensing` (Task 1), `ev.Condensing` (Task 2).
- Produces: `SessionDriver._on_pre_compact(self, input=None, tool_use_id=None, ctx=None) -> dict` (async); `self._condensing: bool`. `_dash_signals()` now passes `condensing=self._condensing`.

- [ ] **Step 1: Write the failing test.** Add to `server/tests/test_driver_status.py` (reuse the file's existing driver-construction helper / `factory` pattern; build a driver `d`):

```python
import asyncio
import server.sessions.events as ev


def test_pre_compact_sets_condensing_status(make_driver):  # adapt to file's fixture/helper
    d = make_driver(messages=[])

    async def go():
        await d._on_pre_compact()
        return d.current_status()

    assert asyncio.run(go()) == "condensing"


def test_condensing_event_clears_flag(make_driver):
    d = make_driver(messages=[])

    async def go():
        await d._on_pre_compact()
        before = d.current_status()
        d._condensing = False  # simulating drain consuming Condensing(active=False)
        return before, d.current_status()

    before, after = asyncio.run(go())
    assert before == "condensing"
    assert after in ("idle", "in_progress", "done")
```

> Implementer note: match the existing construction style in `test_driver_status.py` (it defines a local `factory`). If there is no `make_driver` helper, construct the driver inline exactly as the existing tests in that file do, then exercise `_on_pre_compact`.

- [ ] **Step 2: Run the tests, verify they fail.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_driver_status.py -q`
Expected: FAIL — `'SessionDriver' object has no attribute '_on_pre_compact'`.

- [ ] **Step 3: Implement.** In `server/sessions/driver.py`:

(a) In `__init__`, alongside `self._turn_active: bool = False`:

```python
        self._condensing: bool = False
```

(b) Add the callback method (near `_emit_status`):

```python
    async def _on_pre_compact(self, input=None, tool_use_id=None, ctx=None) -> dict:
        """SDK PreCompact hook: context auto-compaction is starting. Flip the
        transient flag and surface the ``condensing`` status. Never blocks
        compaction; returns an empty HookJSONOutput."""
        self._condensing = True
        await self._emit_status()
        return {}
```

(c) In `_dash_signals`, pass the flag:

```python
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(perms),
            pending_question=bool(self._qreg.pending(self.session_id)),
            pending_plan=any(p.get("tool") == "ExitPlanMode" for p in perms),
            turns_completed=self._turns_completed,
            last_turn_outcome=self._last_turn_outcome,
            condensing=self._condensing,
        )
```

(d) In `_drain`, inside `for e in self._tr.feed(msg):`, right after the `ev.RateLimit` block, add:

```python
                    if isinstance(e, ev.Condensing):
                        # Internal: toggle the transient flag; UI learns via status.
                        self._condensing = e.active
                        await self._emit_status()
                        continue
```

(e) In `_drain`'s `finally:` block, immediately after `await self._call_sync_back()`, add the backstop so the flag never outlives a turn:

```python
            # Compaction is turn-scoped; never let a condensing flag outlive the turn.
            self._condensing = False
```

- [ ] **Step 4: Run the tests, verify they pass (plus the whole driver-status file).**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_driver_status.py server/tests/test_driver.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add server/sessions/driver.py server/tests/test_driver_status.py
git commit -m "feat(driver): condensing flag via PreCompact callback + compact_boundary"
```

---

## Task 4: Factory threading — register the PreCompact hook

**Files:**
- Modify: `server/sessions/sdk_factory.py`, `server/sessions/orchestrator.py`, `server/sessions/driver.py` (2 call sites), `server/tests/fakes/fake_factory.py`, and 4 inline test factories: `server/tests/test_driver_status.py` (3 sites), `server/tests/test_driver.py`, `server/tests/test_command_driver.py` (2 sites), `server/tests/test_driver_recovery.py`.
- Test: `server/tests/test_sdk_factory.py` (create if absent)

**Interfaces:**
- Produces: `real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None)` registers `hooks={"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}` when given a callback. The orchestrator wrapper and all fake factories accept and forward/ignore `on_pre_compact`.

- [ ] **Step 1: Write the failing test.** Create `server/tests/test_sdk_factory.py`:

```python
from server.sessions.sdk_factory import real_sdk_factory


async def _cb(*a, **k):
    return {}


def test_factory_registers_precompact_hook_when_callback_given():
    client = real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None, on_pre_compact=_cb)
    hooks = client.options.hooks
    assert hooks is not None and "PreCompact" in hooks
    matcher = hooks["PreCompact"][0]
    assert _cb in matcher.hooks


def test_factory_no_hooks_without_callback():
    client = real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None)
    assert client.options.hooks is None
```

> Implementer note: `ClaudeSDKClient` stores its options as `.options` (verify the attribute name on the installed SDK; if it differs, assert against the constructed `ClaudeAgentOptions` via a thin local seam — e.g. factor option-building into a helper `_build_options(...)` and test that instead). This keeps the test from depending on a private attribute.

- [ ] **Step 2: Run the test, verify it fails.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_sdk_factory.py -q`
Expected: FAIL — `real_sdk_factory() got an unexpected keyword argument 'on_pre_compact'`.

- [ ] **Step 3: Implement the real factory.** In `server/sessions/sdk_factory.py`, update `real_sdk_factory`:

```python
def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
    ClaudeSDKClient, ClaudeAgentOptions = _import_sdk()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    if extra_env:
        env.update(extra_env)
    hooks = None
    if on_pre_compact is not None:
        from claude_agent_sdk import HookMatcher
        hooks = {"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
        hooks=hooks,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": COMPLETION_INSTRUCTION,
        },
    )
    return ClaudeSDKClient(options=opts)
```

> If `HookMatcher` is not exported from `claude_agent_sdk` top-level, import it from `claude_agent_sdk.types`. Verify with `python -c "from claude_agent_sdk import HookMatcher"`.

- [ ] **Step 4: Thread through the orchestrator wrapper.** In `server/sessions/orchestrator.py`, `_session_factory`:

```python
        def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
            extra = x11.env_for(session_id) if x11 else {}
            if extra:
                return base(cwd, config_dir, can_use_tool, mode, resume,
                            extra_env=extra, on_pre_compact=on_pre_compact)
            return base(cwd, config_dir, can_use_tool, mode, resume, on_pre_compact=on_pre_compact)
```

- [ ] **Step 5: Pass the callback from the driver.** In `server/sessions/driver.py`, both factory call sites:

```python
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode,
                                     self.resume, on_pre_compact=self._on_pre_compact)
```

and (the resume-less rebuild site):

```python
        client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, None,
                               on_pre_compact=self._on_pre_compact)
```

- [ ] **Step 6: Make fake factories accept the kwarg.** In `server/tests/fakes/fake_factory.py`:

```python
    def make(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(list(msgs), on_can_use_tool=can_use_tool)
```

And in each inline `def factory(cwd, config_dir, can_use_tool, mode, resume):` listed in **Files** (7 sites across `test_driver_status.py`, `test_driver.py`, `test_command_driver.py`, `test_driver_recovery.py`), add the keyword-only param:

```python
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
```

- [ ] **Step 7: Run the new test + the full driver/orchestrator suites.**

Run: `PYTHONPATH=<worktree>/src <main>/.venv/bin/python -m pytest server/tests/test_sdk_factory.py server/tests/test_driver_status.py server/tests/test_driver.py server/tests/test_command_driver.py server/tests/test_driver_recovery.py server/tests/test_orchestrator_questions.py -q`
Expected: PASS (no `unexpected keyword argument` errors anywhere).

- [ ] **Step 8: Run ruff + mypy on touched backend files, then the full backend suite.**

Run: `cd <worktree> && <main>/.venv/bin/python -m ruff check server/ && <main>/.venv/bin/python -m mypy server/sessions && PYTHONPATH=src <main>/.venv/bin/python -m pytest server/tests/ -q`
Expected: clean (modulo the 2 known env-only failures noted in CLAUDE.md memory: `remote_mode_wiring`, `x11`).

- [ ] **Step 9: Commit.**

```bash
git add server/sessions/sdk_factory.py server/sessions/orchestrator.py server/sessions/driver.py server/tests/fakes/fake_factory.py server/tests/test_sdk_factory.py server/tests/test_driver_status.py server/tests/test_driver.py server/tests/test_command_driver.py server/tests/test_driver_recovery.py
git commit -m "feat(sdk): register PreCompact hook through the session factory"
```

---

## Task 5: Frontend — `condensing` status presentation

**Files:**
- Modify: `web/src/session/statusTier.ts`
- Test: `web/src/session/statusTier.test.ts` (create if absent)

**Interfaces:**
- Produces: `statusPresentation('condensing')` → `{ tier: 'busy', label: 'Condensing…', Icon: Minimize2, textClass: BUSY }`.

- [ ] **Step 1: Write the failing test.** In `web/src/session/statusTier.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier } from './statusTier'

describe('condensing status', () => {
  it('presents as a busy-tier Condensing pill', () => {
    const p = statusPresentation('condensing')
    expect(p.tier).toBe('busy')
    expect(p.label).toBe('Condensing…')
  })
  it('is grouped with the busy tier', () => {
    expect(statusTier('condensing')).toBe('busy')
  })
})
```

- [ ] **Step 2: Run the test, verify it fails.**

Run (in `web/`): `npx vitest run src/session/statusTier.test.ts`
Expected: FAIL — label is the FALLBACK 'Ready', tier 'your-turn'.

- [ ] **Step 3: Implement.** In `web/src/session/statusTier.ts`, add `Minimize2` to the lucide import line, and add the row to `PRESENTATION` (place it right after `in_progress`/`working`):

```ts
  condensing:       { tier: 'busy',      label: 'Condensing…',       Icon: Minimize2,     textClass: BUSY },
```

- [ ] **Step 4: Run the test, verify it passes.**

Run (in `web/`): `npx vitest run src/session/statusTier.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add web/src/session/statusTier.ts web/src/session/statusTier.test.ts
git commit -m "feat(web): condensing status presentation (busy tier)"
```

---

## Task 6: Frontend — WorkingIndicator phrase override + StatusBar wiring

**Files:**
- Modify: `web/src/components/session/WorkingIndicator.tsx`, `web/src/components/session/StatusBar.tsx`
- Test: `web/src/components/session/__tests__/WorkingIndicator.test.tsx` (create if absent), and the existing StatusBar test if present.

**Interfaces:**
- Consumes: the `condensing` status string (Task 5).
- Produces: `WorkingIndicator` accepts optional `phrase?: string` (overrides the whimsical word + the sr-only label). `StatusBar` passes `phrase="Condensing context"` when `status.state === 'condensing'`.

- [ ] **Step 1: Write the failing test.** In `web/src/components/session/__tests__/WorkingIndicator.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { WorkingIndicator } from '../WorkingIndicator'

describe('WorkingIndicator phrase override', () => {
  it('shows the override phrase and keeps the elapsed readout', () => {
    render(<WorkingIndicator phrase="Condensing context" />)
    expect(screen.getByText(/Condensing context…/)).toBeInTheDocument()
    expect(screen.getByTestId('working-elapsed')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test, verify it fails.**

Run (in `web/`): `npx vitest run src/components/session/__tests__/WorkingIndicator.test.tsx`
Expected: FAIL — `phrase` not a prop; whimsical word shown instead.

- [ ] **Step 3: Implement the override.** In `web/src/components/session/WorkingIndicator.tsx`, add `phrase?: string` to `Props`, then make it win over the whimsical word but below the sub-agent phrase:

```tsx
  const subAgentPhrase = subAgentCount > 0
    ? (subAgentCount === 1 ? 'Running a sub-agent' : `Running ${subAgentCount} sub-agents`)
    : null
  const phraseText = subAgentPhrase ?? phrase ?? word
  const srLabel = subAgentPhrase
    ? `${subAgentPhrase}…`
    : phrase
      ? `${phrase}…`
      : (count > 0 ? `Working, ${count} queued` : 'Working…')
```

Update the render to use `phraseText` where `phrase` (the whimsical word) was previously interpolated (`{phraseText}…`). Keep the destructured prop name distinct from the local word to avoid shadowing — destructure as `phrase` in Props and rename the `useState` word var if needed for clarity.

- [ ] **Step 4: Wire StatusBar.** In `web/src/components/session/StatusBar.tsx`, in the busy branch:

```tsx
      {tier === 'busy' ? (
        <WorkingIndicator
          count={status.count}
          subAgentCount={subAgentCount}
          phrase={status.state === 'condensing' ? 'Condensing context' : undefined}
        />
      ) : (
```

- [ ] **Step 5: Run the test, verify it passes.**

Run (in `web/`): `npx vitest run src/components/session/__tests__/WorkingIndicator.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add web/src/components/session/WorkingIndicator.tsx web/src/components/session/StatusBar.tsx web/src/components/session/__tests__/WorkingIndicator.test.tsx
git commit -m "feat(web): Condensing context phrase in the StatusBar spinner"
```

---

## Task 7: Frontend — context severity helper

**Files:**
- Modify: `web/src/lib/contextWindow.ts`
- Test: `web/src/lib/contextWindow.test.ts` (existing)

**Interfaces:**
- Produces: `contextSeverity(remainingPct: number | null): 'neutral' | 'warning' | 'danger'` — `null`→neutral, `>25`→neutral, `(10,25]`→warning, `<=10`→danger.

- [ ] **Step 1: Write the failing test.** Add to `web/src/lib/contextWindow.test.ts`:

```ts
import { contextSeverity } from './contextWindow'

describe('contextSeverity', () => {
  it('is neutral when plenty remains or unknown', () => {
    expect(contextSeverity(null)).toBe('neutral')
    expect(contextSeverity(80)).toBe('neutral')
    expect(contextSeverity(26)).toBe('neutral')
  })
  it('warns in the mid band', () => {
    expect(contextSeverity(25)).toBe('warning')
    expect(contextSeverity(11)).toBe('warning')
  })
  it('is danger when nearly full', () => {
    expect(contextSeverity(10)).toBe('danger')
    expect(contextSeverity(2)).toBe('danger')
  })
})
```

- [ ] **Step 2: Run the test, verify it fails.**

Run (in `web/`): `npx vitest run src/lib/contextWindow.test.ts`
Expected: FAIL — `contextSeverity` not exported.

- [ ] **Step 3: Implement.** Append to `web/src/lib/contextWindow.ts`:

```ts
export type ContextSeverity = 'neutral' | 'warning' | 'danger'

/**
 * Severity for a remaining-context percentage (100 = window empty). Drives the
 * color escalation on the session's `ctx %` readout. `null` (no usage yet) is
 * neutral so the readout stays calm until there is real data.
 */
export function contextSeverity(remainingPct: number | null): ContextSeverity {
  if (remainingPct == null) return 'neutral'
  if (remainingPct <= 10) return 'danger'
  if (remainingPct <= 25) return 'warning'
  return 'neutral'
}
```

- [ ] **Step 4: Run the test, verify it passes.**

Run (in `web/`): `npx vitest run src/lib/contextWindow.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add web/src/lib/contextWindow.ts web/src/lib/contextWindow.test.ts
git commit -m "feat(web): contextSeverity helper for the context readout"
```

---

## Task 8: Frontend — SessionMeta color escalation + hint

**Files:**
- Modify: `web/src/components/session/SessionMeta.tsx`
- Test: `web/src/components/session/__tests__/SessionMeta.test.tsx` (create if absent; if `SessionMeta` is hard to render standalone due to many props, test via the smallest prop set that renders the ctx span)

**Interfaces:**
- Consumes: `contextSeverity` (Task 7); the existing `contextPct` prop on `SessionMeta`.

- [ ] **Step 1: Write the failing test.** In `web/src/components/session/__tests__/SessionMeta.test.tsx`, render `SessionMeta` with `contextPct={5}` and the other required props at minimal/empty values (copy the prop list from the component signature), then assert:

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionMeta } from '../SessionMeta'

const base = {
  account: 'a', model: 'claude', mode: 'interactive', quota: null,
  subAgents: 0, statusSlot: null, exportSlot: null, models: [], accounts: [],
  accountPinned: false, onSelectModel: () => {}, onSelectAccount: () => {}, onSetMode: () => {},
} as const

describe('SessionMeta context escalation', () => {
  it('marks the ctx readout as danger and hints when nearly full', () => {
    render(<SessionMeta {...base} contextPct={5} />)
    const ctx = screen.getByLabelText('Remaining context')
    expect(ctx.className).toMatch(/red/)
    expect(screen.getByText(/compacts soon/i)).toBeInTheDocument()
  })
  it('stays calm with plenty of context', () => {
    render(<SessionMeta {...base} contextPct={80} />)
    const ctx = screen.getByLabelText('Remaining context')
    expect(ctx.className).not.toMatch(/red|amber/)
  })
})
```

> Implementer note: adapt `base` to the EXACT current prop names/types of `SessionMeta` (see `SessionMeta.tsx:66`). Do not invent props.

- [ ] **Step 2: Run the test, verify it fails.**

Run (in `web/`): `npx vitest run src/components/session/__tests__/SessionMeta.test.tsx`
Expected: FAIL — no color class, no hint.

- [ ] **Step 3: Implement.** In `web/src/components/session/SessionMeta.tsx`, import the helper and apply it to the existing ctx block (around lines 114-120):

```tsx
import { contextSeverity } from '@/lib/contextWindow'
```

```tsx
        {/* Remaining context */}
        {(() => {
          const sev = contextSeverity(contextPct)
          const color = sev === 'danger'
            ? 'text-red-700 dark:text-red-400'
            : sev === 'warning'
              ? 'text-amber-700 dark:text-amber-400'
              : 'text-gray-900 dark:text-gray-100'
          return (
            <span className={cn('inline-flex items-center gap-1', color)} aria-label="Remaining context">
              <span className="text-gray-400 dark:text-gray-500">ctx</span>
              <span className="font-medium tabular-nums">
                {contextPct != null ? `${contextPct}%` : DASH}
              </span>
              {sev === 'danger' && <span className="opacity-70">· compacts soon</span>}
            </span>
          )
        })()}
```

> Use the file's existing `cn` import (add it if not already imported). Keep `aria-label="Remaining context"` on the outer span so the test selector and screen readers both work.

- [ ] **Step 4: Run the test, verify it passes.**

Run (in `web/`): `npx vitest run src/components/session/__tests__/SessionMeta.test.tsx`
Expected: PASS.

- [ ] **Step 5: Full web gates.**

Run (in `web/`): `npx tsc -b --force && npx vitest run && npx eslint src`
Expected: clean (0 eslint warnings). Note CLAUDE.md memory: some react-router-dom/@novnc tests are CI-only in an offline worktree — those failures, if present, are environmental.

- [ ] **Step 6: Commit.**

```bash
git add web/src/components/session/SessionMeta.tsx web/src/components/session/__tests__/SessionMeta.test.tsx
git commit -m "feat(web): escalate ctx readout color + compacts-soon hint"
```

---

## Final verification (before PR)

- [ ] Backend: `cd <worktree> && <main>/.venv/bin/python -m ruff check server/ && <main>/.venv/bin/python -m mypy server/sessions && PYTHONPATH=src <main>/.venv/bin/python -m pytest server/tests/ -q` (modulo the 2 known env-only failures).
- [ ] Web (in `web/`, node_modules symlinked from the deploy checkout per CLAUDE.md memory): `npx tsc -b --force && npx vitest run && npx eslint src`.
- [ ] Manual sanity: a session whose status becomes `condensing` shows "Condensing context…" + spinner on the card pill and in the StatusBar; the `ctx %` readout turns amber then red as it drops, with "· compacts soon" under ~10%.

## Self-review notes (author)

- **Spec coverage:** Piece 1 start hook → Task 4; end boundary → Task 2; status pipeline → Tasks 1,3,5,6; Piece 2 escalation → Tasks 7,8. No-persist rule → Task 3 (steps 3a/3e), Global Constraints. Non-goal (no ETA) honored — no progress-bar task exists by design.
- **Type consistency:** `condensing` field name, `ev.Condensing(active/trigger/pre_tokens)`, `_on_pre_compact`, `contextSeverity`, `phrase` prop — used identically across producing and consuming tasks.
- **Known risks flagged inline:** `ClaudeSDKClient.options` attribute name (Task 4 seam note), `HookMatcher` import path (Task 4), `SessionMeta` exact prop list (Task 8).
