# Session Status Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a session's state unmistakable everywhere it is shown — BUSY vs NEEDS-YOU-NOW vs YOUR-TURN — by collapsing the two divergent status systems onto one rich status, and replaying it on WebSocket attach so the in-session header can never go stale.

**Architecture:** The backend already derives a rich status (`derive_status` in `server/sessions/status.py`). Today it ships that status over two channels with two vocabularies: persisted+polled for the session list, and an ephemeral `working`/`idle` WebSocket event for the in-session `StatusBar`. We unify: the driver emits the **rich** status as the live `status` event on every transition, the WS attach path **replays** the current status, and a single frontend helper (`statusTier` + presentation map) drives both the list pill and the `StatusBar`.

**Tech Stack:** Python 3 / FastAPI / `claude_agent_sdk` (backend, `pytest`); React + Vite + TypeScript + Tailwind + lucide-react (frontend, `vitest` + `tsc --noEmit` + `eslint`).

## Global Constraints

- **Single source of truth (Constitution VI):** one status vocabulary and one frontend presentation map. No second copy of the status→label/color mapping after this work.
- **Test commands:** backend → `pytest server/tests/` (from repo root); web → run **in `web/`** → `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`.
- **Worktree env:** a fresh worktree has no built env. Run backend tests against the main checkout's prebuilt interpreter: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/…`. Symlink `web/node_modules` from the main checkout if absent: `ln -s /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/web/node_modules web/node_modules`.
- **API auth in tests:** WS/API tests use identity header `Tailscale-User-Login: wstest@example.com` (already wired in `test_ws.py`'s `ws_env`). For non-WS suites that 401, run `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest …`.
- **Status vocabulary (rich, authoritative):** `idle`, `in_progress`, `waiting_input`, `waiting_question`, `ready_review`, `done`, `complete`, `needs_attention`, `limited`, `ended`. (`waiting_question` is added by this plan; `limited`/`ended` are orchestrator overlays, not produced by `derive_status`.)
- **Tier mapping (authoritative):**
  - `busy` ← `in_progress`
  - `needs-you` ← `waiting_input`, `waiting_question`, `ready_review`
  - `your-turn` ← `done`, `complete`, `idle`
  - `error` ← `needs_attention`, `limited`, `ended`
- **Labels:** `in_progress`→"Working…"; `ready_review`→"Approve plan"; `waiting_input`→"Permission needed"; `waiting_question`→"Answer needed"; `done`/`complete`→"Your turn"; `idle`→"Ready"; `needs_attention`→"Turn failed"; `limited`→"Usage limit"; `ended`→"Ended".
- **Reduced motion:** pulsing pills and the spinner must fall back to static under `prefers-reduced-motion` (the existing `WorkingIndicator` already honors this).
- **Accessibility:** `StatusBar` keeps `role="status"` / `aria-live="polite"`; the connection note ("Reconnecting…"/"Disconnected") behavior is unchanged.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Add `waiting_question` to the rich status derivation

Splitting `waiting_input` into permission-pending vs question-pending lets both surfaces show the sub-type label the user picked ("Permission needed" vs "Answer needed") with zero frontend reducer surgery. `derive_status` is pure and table-tested.

**Files:**
- Modify: `server/sessions/status.py:23-36`
- Test: `server/tests/test_status.py` (existing table tests — locate the file; if absent, the test lives wherever `derive_status` is currently tested — `grep -rl derive_status server/tests`)

**Interfaces:**
- Produces: `derive_status(StatusSignals)` now returns `"waiting_question"` when `pending_question` is true (and no pending plan), `"waiting_input"` when `pending_permission` is true (and no pending plan/question).

- [ ] **Step 1: Write the failing test**

Add to the `derive_status` test module (alongside the existing cases):

```python
from server.sessions.status import StatusSignals, derive_status

def test_pending_question_is_waiting_question():
    sig = StatusSignals(turn_active=True, pending_permission=False,
                        pending_question=True, pending_plan=False, turns_completed=0)
    assert derive_status(sig) == "waiting_question"

def test_pending_permission_is_waiting_input():
    sig = StatusSignals(turn_active=True, pending_permission=True,
                        pending_question=False, pending_plan=False, turns_completed=0)
    assert derive_status(sig) == "waiting_input"

def test_pending_plan_beats_question():
    sig = StatusSignals(turn_active=True, pending_permission=True,
                        pending_question=True, pending_plan=True, turns_completed=0)
    assert derive_status(sig) == "ready_review"
```

- [ ] **Step 2: Run the tests to verify the first one fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_status.py -v` (adjust path to the actual test file)
Expected: `test_pending_question_is_waiting_question` FAILS (returns `"waiting_input"`).

- [ ] **Step 3: Implement the split**

In `server/sessions/status.py`, change the body of `derive_status` so the question case precedes the permission case:

```python
def derive_status(sig: StatusSignals) -> str:
    if sig.pending_plan:
        return "ready_review"
    if sig.pending_question:
        return "waiting_question"
    if sig.pending_permission:
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

Also update the module docstring's precedence line to mention the question split.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_status.py -v`
Expected: PASS (all, including any pre-existing cases — if a pre-existing case asserted `waiting_input` for a question-only signal, update it to `waiting_question`).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/status.py server/tests/test_status.py
git commit -m "feat(status): split waiting_question from waiting_input in derive_status

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Unify the driver's live status onto the rich vocabulary

`_put_status(state, count)` currently emits a binary `working`/`idle` `ev.Status` and *separately* refreshes the dashboard. Other transition points (`_emit_dashboard_status` at permission/question boundaries) update only the list, never the live `StatusBar`. We collapse both into one `_emit_status()` that emits the rich status to **both** consumers on every transition.

**Files:**
- Modify: `server/sessions/driver.py` (methods `_put_status` :67-71, `_emit_dashboard_status` :196-206, and all call sites: :136, :144, :220, :223, :239, :254, :327, :339, :358)
- Test: `server/tests/test_driver_status.py` (new)

**Interfaces:**
- Consumes: `derive_status` / `StatusSignals` from Task 1.
- Produces:
  - `SessionDriver._emit_status(count: int | None = None) -> None` — emits `ev.Status(state=<rich>, count=<queue depth>)` and calls `on_status(session_id, <rich>)`. `count` defaults to `len(self._queue)`.
  - `SessionDriver.current_status() -> str` — returns `derive_status(self._dash_signals())` (no side effects). Used by the WS attach path in Task 3.
  - `ev.Status.state` now carries rich vocabulary values, not `"working"`/`"idle"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_driver_status.py`. This drives a `SessionDriver` with the existing `FakeSDKClient` and asserts the live `ev.Status` events carry rich values.

```python
from __future__ import annotations
import asyncio
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


class _OneTurnClient(FakeSDKClient):
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("hi", uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def _make_driver():
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        return _OneTurnClient(can_use_tool=can_use_tool)
    return SessionDriver(
        "sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
        sdk_factory=factory, registry=PermissionRegistry(),
        identity="wstest@example.com",
    )


@pytest.mark.asyncio
async def test_live_status_uses_rich_vocabulary():
    d = _make_driver()
    await d.start()
    statuses: list[str] = []

    async def collect():
        async for e in d.events():
            if isinstance(e, ev.Status):
                statuses.append(e.state)
            if isinstance(e, ev.TurnEnd):
                break

    await d.send_prompt("go")
    await asyncio.wait_for(collect(), timeout=5.0)
    await d.aclose()

    # No legacy 'working'/'idle' wire values; the active turn reports in_progress.
    assert "working" not in statuses and "idle" not in statuses
    assert "in_progress" in statuses


@pytest.mark.asyncio
async def test_current_status_reflects_idle_before_first_turn():
    d = _make_driver()
    await d.start()
    assert d.current_status() == "idle"
    await d.aclose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_status.py -v`
Expected: FAIL — `current_status` is undefined / `statuses` contains `"working"`.

- [ ] **Step 3: Implement `_emit_status` + `current_status`, replace call sites**

In `server/sessions/driver.py`, replace `_put_status` (67-71) and `_emit_dashboard_status` (196-206) with:

```python
    def current_status(self) -> str:
        """The rich derived status right now (no side effects). Used on WS attach."""
        return derive_status(self._dash_signals())

    async def _emit_status(self, count: int | None = None) -> None:
        """Single status emitter — rich vocabulary to BOTH consumers.

        Emits a live Status event (for the in-session StatusBar) AND notifies the
        orchestrator (which persists it for the session LIST). `count` is the queue
        depth shown in the busy label; defaults to the live queue length.
        Best-effort on the callback: a failure must never break the session loop.
        """
        status = derive_status(self._dash_signals())
        n = len(self._queue) if count is None else count
        await self._out.put(ev.Status(state=status, count=n))
        if self._on_status is not None:
            try:
                await self._on_status(self.session_id, status)
            except Exception:
                log.warning("driver.on_status failed session=%s", self.session_id, exc_info=True)
```

Then replace every call site:
- `await self._put_status("working", len(self._queue))` (lines 239, 254, 339) → `await self._emit_status()`
- `await self._put_status("idle", 0)` (lines 327, 358) → `await self._emit_status()`
- `await self._emit_dashboard_status()` (lines 136, 144, 220, 223) → `await self._emit_status()`

Confirm the docstring on `_on_status` (lines 46-49) still reads correctly (it already describes the rich vocabulary).

- [ ] **Step 4: Run the test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_status.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing driver/WS suites to catch regressions**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py -v`
Expected: PASS. (If any test asserts a `status` frame with `state == "working"`/`"idle"`, update it to the rich value — `in_progress`/`done`. Search: `grep -n "working\|idle" server/tests/test_ws.py`.)

- [ ] **Step 6: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_status.py server/tests/test_ws.py
git commit -m "feat(driver): emit rich status to StatusBar + list on every transition

Collapses the binary working/idle live status and the dashboard refresh into
one _emit_status() that ships derive_status() to both consumers. Adds
current_status() for the WS attach replay.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Replay the current live status on WebSocket attach (the bug fix)

The attach path replays the transcript and re-emits pending permissions/questions but never the live status, so a client attaching mid-turn keeps its initial `idle` → "Ready". Emit the current status right after the question re-emit.

**Files:**
- Modify: `server/bridge/ws.py` (attach sequence, after the pending-question re-emit at ~line 192, before the deferred-initial-prompt pop at ~line 200)
- Test: `server/tests/test_ws.py` (new test, modeled on `test_reattach_reemits_pending_question`)

**Interfaces:**
- Consumes: `driver.current_status()` and `driver.queued()` from Task 2 (`queued()` already exists, returns `list[str]`).
- Produces: on every attach, a `status` frame is sent after `replayComplete`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_ws.py` (it already defines `_QuestionBlockingFakeClient`, `ws_env`, `_drain_events`, `_IDENTITY_HEADERS`):

```python
def test_attach_emits_live_status(ws_env):
    """Attaching to a session whose driver is blocked mid-turn emits a live status
    frame so the in-session header reflects the true state (regression: stale 'Ready')."""
    _app, orch, client = ws_env([], client_cls=_QuestionBlockingFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-attach-status", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")
        # Drive the turn until it blocks on the question.
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        _drain_events(ws, until_type="question", timeout=8.0)
        ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))

    # Reattach — a status frame must now be emitted, carrying the blocked state.
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="status", timeout=6.0)
        status_evs = [e for e in events if e.get("type") == "status"]
        assert status_evs, f"no status frame on attach — {[e.get('type') for e in events]}"
        assert status_evs[-1]["state"] in ("waiting_question", "waiting_input"), (
            f"expected a needs-you state, got {status_evs[-1]['state']}"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py::test_attach_emits_live_status -v`
Expected: FAIL — no `status` frame is sent on attach.

- [ ] **Step 3: Implement the attach status emit**

In `server/bridge/ws.py`, immediately after the pending-question re-emit loop (the block that ends around line 192, using the already-imported `from server.sessions import events as ev`) and before the deferred-initial-prompt pop, add:

```python
            # 2d) Emit the current live status so a client attaching or reconnecting
            #     mid-turn renders the true state immediately. The live status is
            #     otherwise only pushed on transitions, so without this a mid-turn
            #     attach would show the reducer's initial 'idle' ("Ready"). Best-effort.
            try:
                await _send(redact_event(ev.Status(
                    state=driver.current_status(),
                    count=len(driver.queued()),
                ).to_wire()))
            except Exception:
                log.warning("ws.attach: live status emit failed for %s", session_id, exc_info=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py::test_attach_emits_live_status -v`
Expected: PASS.

- [ ] **Step 5: Run the full WS suite**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ws.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws.py
git commit -m "fix(ws): replay live status on attach (kills stale 'Ready' mid-turn)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Frontend single source of truth — `statusTier` + presentation map

One module maps every rich status to a tier + label + icon + color. Both the list pill and the `StatusBar` consume it. This is where the parallel `SESSION_STATUS_MAP` and `statusText()` get eliminated (Tasks 5–6).

**Files:**
- Create: `web/src/session/statusTier.ts`
- Modify: `web/src/api/types.ts:24-33` (add `'waiting_question'` to `SessionStatus`)
- Test: `web/src/session/__tests__/statusTier.test.ts` (new)

**Interfaces:**
- Produces:
  - `export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'error'`
  - `export function statusTier(state: string): StatusTier`
  - `export interface StatusPresentation { tier: StatusTier; label: string; Icon: ComponentType<{ className?: string; size?: number }>; textClass: string }`
  - `export function statusPresentation(state: string): StatusPresentation`

- [ ] **Step 1: Write the failing test**

Create `web/src/session/__tests__/statusTier.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { statusTier, statusPresentation } from '../statusTier'

describe('statusTier', () => {
  it.each([
    ['in_progress', 'busy'],
    ['waiting_input', 'needs-you'],
    ['waiting_question', 'needs-you'],
    ['ready_review', 'needs-you'],
    ['done', 'your-turn'],
    ['complete', 'your-turn'],
    ['idle', 'your-turn'],
    ['needs_attention', 'error'],
    ['limited', 'error'],
    ['ended', 'error'],
    ['working', 'busy'],            // legacy alias tolerated
  ])('maps %s -> %s', (state, tier) => {
    expect(statusTier(state)).toBe(tier)
  })

  it('falls back to your-turn for an unknown state', () => {
    expect(statusTier('totally-unknown')).toBe('your-turn')
  })
})

describe('statusPresentation', () => {
  it.each([
    ['in_progress', 'Working…'],
    ['ready_review', 'Approve plan'],
    ['waiting_input', 'Permission needed'],
    ['waiting_question', 'Answer needed'],
    ['done', 'Your turn'],
    ['idle', 'Ready'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
    ['ended', 'Ended'],
  ])('labels %s as "%s"', (state, label) => {
    expect(statusPresentation(state).label).toBe(label)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/session/__tests__/statusTier.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the module + extend the type**

Add `'waiting_question'` to `SessionStatus` in `web/src/api/types.ts`:

```typescript
export type SessionStatus =
  | 'idle'
  | 'in_progress'
  | 'waiting_input'
  | 'waiting_question'
  | 'ready_review'
  | 'done'
  | 'complete'
  | 'needs_attention'
  | 'limited'
  | 'ended'
```

Create `web/src/session/statusTier.ts`:

```typescript
import {
  Loader2, CircleAlert, HelpCircle, Eye, Check, CheckCircle2,
  AlertTriangle, X, Clock,
} from 'lucide-react'
import type { ComponentType } from 'react'

export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'error'

export interface StatusPresentation {
  tier: StatusTier
  label: string
  Icon: ComponentType<{ className?: string; size?: number }>
  /** Tailwind text-color classes (light + dark). */
  textClass: string
}

const NEEDS_YOU = 'text-amber-700 dark:text-amber-400'
const ERROR = 'text-red-700 dark:text-red-400'
const BUSY = 'text-emerald-700 dark:text-emerald-400'
const CALM = 'text-gray-600 dark:text-gray-400'

// One table, keyed by the rich status string. 'working' is a tolerated legacy alias.
const PRESENTATION: Record<string, StatusPresentation> = {
  in_progress:      { tier: 'busy',      label: 'Working…',          Icon: Loader2,      textClass: BUSY },
  working:          { tier: 'busy',      label: 'Working…',          Icon: Loader2,      textClass: BUSY },
  ready_review:     { tier: 'needs-you', label: 'Approve plan',      Icon: Eye,          textClass: NEEDS_YOU },
  waiting_input:    { tier: 'needs-you', label: 'Permission needed', Icon: CircleAlert,  textClass: NEEDS_YOU },
  waiting_question: { tier: 'needs-you', label: 'Answer needed',     Icon: HelpCircle,   textClass: NEEDS_YOU },
  done:             { tier: 'your-turn', label: 'Your turn',         Icon: Check,        textClass: CALM },
  complete:         { tier: 'your-turn', label: 'Your turn',         Icon: CheckCircle2, textClass: CALM },
  idle:             { tier: 'your-turn', label: 'Ready',             Icon: Clock,        textClass: CALM },
  needs_attention:  { tier: 'error',     label: 'Turn failed',       Icon: AlertTriangle, textClass: ERROR },
  limited:          { tier: 'error',     label: 'Usage limit',       Icon: AlertTriangle, textClass: ERROR },
  ended:            { tier: 'error',     label: 'Ended',             Icon: X,            textClass: CALM },
}

const FALLBACK: StatusPresentation = {
  tier: 'your-turn', label: 'Ready', Icon: Clock, textClass: CALM,
}

export function statusPresentation(state: string): StatusPresentation {
  return PRESENTATION[state] ?? FALLBACK
}

export function statusTier(state: string): StatusTier {
  return statusPresentation(state).tier
}
```

Note: `ended` keeps the calm gray text (it is terminal, not actionable) while still classifying as the `error` tier for sorting; this matches the existing `SESSION_STATUS_MAP` treatment of `ended`.

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/session/__tests__/statusTier.test.ts`
Expected: PASS.

- [ ] **Step 5: Type-check**

Run (in `web/`): `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/statusTier.ts web/src/session/__tests__/statusTier.test.ts web/src/api/types.ts
git commit -m "feat(web): single status tier + presentation map (statusTier)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Render the `StatusBar` by tier

Replace the local `statusText()` switch with the shared presentation. Busy → `WorkingIndicator`; needs-you → amber label; your-turn → calm label; error → red label.

**Files:**
- Modify: `web/src/components/session/StatusBar.tsx`
- Test: `web/src/components/session/__tests__/StatusBar.test.tsx` (update existing assertions)

**Interfaces:**
- Consumes: `statusPresentation`, `statusTier` from Task 4; existing `WorkingIndicator`.

- [ ] **Step 1: Update the test to the rich vocabulary**

In `web/src/components/session/__tests__/StatusBar.test.tsx`, replace the state-specific cases. Keep the connection-note and a11y tests unchanged. New/updated cases:

```typescript
  it('shows "Ready" for idle state', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('renders the WorkingIndicator for in_progress with no queue', () => {
    render(<StatusBar status={{ state: 'in_progress', count: 0 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working…')
  })

  it('renders WorkingIndicator with queued label for in_progress with queued items', () => {
    render(<StatusBar status={{ state: 'in_progress', count: 2 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working, 2 queued')
  })

  it('shows "Permission needed" for waiting_input', () => {
    render(<StatusBar status={{ state: 'waiting_input', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Permission needed')).toBeInTheDocument()
  })

  it('shows "Answer needed" for waiting_question', () => {
    render(<StatusBar status={{ state: 'waiting_question', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Answer needed')).toBeInTheDocument()
  })

  it('shows "Approve plan" for ready_review', () => {
    render(<StatusBar status={{ state: 'ready_review', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Approve plan')).toBeInTheDocument()
  })

  it('shows "Your turn" for done', () => {
    render(<StatusBar status={{ state: 'done', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Your turn')).toBeInTheDocument()
  })

  it('shows "Turn failed" for needs_attention', () => {
    render(<StatusBar status={{ state: 'needs_attention', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Turn failed')).toBeInTheDocument()
  })
```

Delete the old `working`, `waiting-permission`, `queued`, and `custom-state` cases (those wire values no longer occur). Keep the three connection-note tests and the `role="status"`/`aria-live` test, but change their `state: 'idle'` is fine as-is.

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/StatusBar.test.tsx`
Expected: FAIL — e.g. "Permission needed" not found (current code shows raw `waiting_input`).

- [ ] **Step 3: Rewrite `StatusBar.tsx`**

```typescript
import { cn } from '@/lib/utils'
import { WorkingIndicator } from './WorkingIndicator'
import { statusPresentation } from '@/session/statusTier'

interface Props {
  status: { state: string; count: number }
  connectionState: string
}

export function StatusBar({ status, connectionState }: Props) {
  const connected = connectionState === 'connected'
  const connectionNote = connected
    ? null
    : connectionState === 'reconnecting'
      ? 'Reconnecting…'
      : 'Disconnected'

  const { tier, label, Icon, textClass } = statusPresentation(status.state)

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'flex items-center gap-2 px-3 py-1 text-xs',
        tier === 'needs-you' || tier === 'error' ? textClass : 'text-gray-500 dark:text-gray-400',
      )}
    >
      {tier === 'busy' ? (
        <WorkingIndicator count={status.count} />
      ) : (
        <span className={cn('inline-flex items-center gap-1', tier !== 'your-turn' && 'font-medium')}>
          <Icon size={12} aria-hidden="true" />
          {label}
        </span>
      )}
      {connectionNote && (
        <span className="ml-auto text-amber-600 dark:text-amber-400 font-medium">
          {connectionNote}
        </span>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/components/session/__tests__/StatusBar.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/StatusBar.tsx web/src/components/session/__tests__/StatusBar.test.tsx
git commit -m "feat(web): render StatusBar by status tier via shared map

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Point the list `StatusPill` at the shared map + pulse for needs-you/error

Delete the divergent `SESSION_STATUS_MAP` and route `StatusPill`'s session statuses through `statusPresentation`. Account statuses keep their own map. Needs-you/error pills pulse (reduced-motion safe).

**Files:**
- Modify: `web/src/components/UsageBadge.tsx` (remove `SESSION_STATUS_MAP`, lines 97-151; rework `StatusPill`, lines 153-180)
- Test: `web/src/components/__tests__/StatusPill.rich.test.tsx` (update expected labels)

**Interfaces:**
- Consumes: `statusPresentation`, `statusTier` from Task 4.
- Note: `StatusPill` currently accepts `AnyStatus = AccountStatus | SessionStatus`. Keep that. Resolve account statuses via the existing `ACCOUNT_STATUS_MAP`; for everything else fall through to `statusPresentation`.

- [ ] **Step 1: Update the test**

In `web/src/components/__tests__/StatusPill.rich.test.tsx`, change the expected labels to the unified ones:

```typescript
  it.each([
    ['in_progress', 'Working…'],
    ['waiting_input', 'Permission needed'],
    ['waiting_question', 'Answer needed'],
    ['ready_review', 'Approve plan'],
    ['done', 'Your turn'],
    ['needs_attention', 'Turn failed'],
  ])('renders %s as "%s"', (status, label) => {
    render(<StatusPill status={status as SessionStatus} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/StatusPill.rich.test.tsx`
Expected: FAIL — old labels ("In progress", "Waiting for input") no longer rendered.

- [ ] **Step 3: Rework `UsageBadge.tsx`**

Delete `SESSION_STATUS_MAP` (lines 97-151) and the `ALL_STATUS_MAP` merge (153-156). Update imports (drop the now-unused lucide icons that only `SESSION_STATUS_MAP` used: `Check`, `CheckCircle2` stays for accounts, `Loader2`, `Clock`, `Eye`, `X` — verify against remaining usage and let `eslint` flag leftovers). Rewrite `StatusPill`:

```typescript
import { statusPresentation, statusTier } from '@/session/statusTier'
// ...keep ACCOUNT_STATUS_MAP and its StatusConfig type...

interface StatusPillProps {
  status: AnyStatus
  className?: string
}

export function StatusPill({ status, className }: StatusPillProps) {
  // Account statuses have their own vocabulary; session statuses use the shared map.
  const account = (ACCOUNT_STATUS_MAP as Record<string, StatusConfig>)[status]
  const { label, Icon, textClass, pulse } = account
    ? { ...account, pulse: false }
    : (() => {
        const p = statusPresentation(status)
        const tier = statusTier(status)
        return { label: p.label, Icon: p.Icon, textClass: p.textClass, pulse: tier === 'needs-you' || tier === 'error' }
      })()

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium',
        textClass,
        pulse && 'motion-safe:animate-pulse',
        className,
      )}
      aria-label={`Status: ${label}`}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}
```

`motion-safe:animate-pulse` is Tailwind's built-in reduced-motion-safe pulse (no pulse when `prefers-reduced-motion: reduce`).

- [ ] **Step 4: Run the test + lint + type-check**

Run (in `web/`):
```bash
npx vitest run src/components/__tests__/StatusPill.rich.test.tsx
npx tsc --noEmit
npx eslint src/components/UsageBadge.tsx
```
Expected: tests PASS, no type errors, no lint errors (remove any unused icon imports `eslint` flags).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/UsageBadge.tsx web/src/components/__tests__/StatusPill.rich.test.tsx
git commit -m "feat(web): list pill uses shared status map; pulse for needs-you/error

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Sort the session list by tier (stable within tier)

Blocked/error to the top, finished in the middle, busy at the bottom. JS `Array.prototype.sort` is stable, so a tier-rank sort preserves intra-tier order — cards only move when their own tier changes.

**Files:**
- Modify: `web/src/components/SessionList.tsx` (sort `sessions` before mapping)
- Test: `web/src/components/__tests__/SessionList.test.tsx` (add ordering test)

**Interfaces:**
- Consumes: `statusTier` from Task 4.
- Produces: a module-local `TIER_RANK: Record<StatusTier, number>` and the sort applied in `SessionList`.

- [ ] **Step 1: Write the failing test**

Add to `web/src/components/__tests__/SessionList.test.tsx` (follow the file's existing render helpers / `Session` factory; construct minimal sessions with distinct `status`):

```typescript
it('sorts needs-you and error to the top, busy to the bottom, stable within tier', () => {
  const sessions = [
    makeSession({ id: 'a', name: 'A busy',   status: 'in_progress' }),
    makeSession({ id: 'b', name: 'B done',   status: 'done' }),
    makeSession({ id: 'c', name: 'C blocked', status: 'waiting_input' }),
    makeSession({ id: 'd', name: 'D busy2',  status: 'in_progress' }),
    makeSession({ id: 'e', name: 'E error',  status: 'needs_attention' }),
  ]
  render(<SessionList sessions={sessions} accounts={[]} onNew={() => {}} onOpen={() => {}}
                      onRename={() => {}} onClose={() => {}} onDelete={() => {}} />)
  const order = screen.getAllByRole('button', { name: /Open session/ }).map(
    (b) => b.getAttribute('aria-label'),
  )
  // needs-you (C) + error (E) first (original relative order C before E),
  // then your-turn (B), then busy (A before D).
  expect(order).toEqual([
    'Open session C blocked',
    'Open session E error',
    'Open session B done',
    'Open session A busy',
    'Open session D busy2',
  ])
})
```

If the test file has no `makeSession` factory, build the array inline with the minimal `Session` fields the file's existing tests use (copy their shape).

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionList.test.tsx`
Expected: FAIL — current order is the input order.

- [ ] **Step 3: Implement the stable tier sort**

In `web/src/components/SessionList.tsx`, add near the top:

```typescript
import { statusTier, type StatusTier } from '@/session/statusTier'

// Lower rank sorts first: action-required at top, finished next, busy last.
const TIER_RANK: Record<StatusTier, number> = {
  'needs-you': 0,
  error: 1,
  'your-turn': 2,
  busy: 3,
}
```

Inside the component, before the `.map`, sort a copy (never mutate the prop):

```typescript
  const ordered = [...sessions].sort(
    (a, b) => TIER_RANK[statusTier(a.status)] - TIER_RANK[statusTier(b.status)],
  )
```

Then map over `ordered` instead of `sessions`.

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionList.test.tsx`
Expected: PASS.

- [ ] **Step 5: Verify keyboard-nav selection still works**

The `Dashboard` keyboard nav indexes into the **unsorted** `sessions` array from `useSessions`, while the list now renders `ordered`. Confirm `selectedId` (not index) drives the `selected` prop on `SessionCard` — it does (`session.id === selectedId`), so highlight follows the right card regardless of order. Run the existing Dashboard nav tests:

Run (in `web/`): `npx vitest run src/components/__tests__/`
Expected: PASS. If a nav test asserts visual order matches `sessions` index, update it to assert by `selectedId`.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionList.tsx web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(web): sort sessions by tier (needs-you/error top, busy bottom)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Browser tab title reflects the needs-you count

A small hook over the polled session list sets `document.title` to `(N) ccswitch — needs you` when N>0 sessions are blocked, else `ccswitch`.

**Files:**
- Create: `web/src/hooks/useNeedsYouTitle.ts`
- Modify: `web/src/components/Dashboard.tsx` (call the hook where `useSessions()` data is available, ~line 1027)
- Test: `web/src/hooks/__tests__/useNeedsYouTitle.test.ts` (new)

**Interfaces:**
- Consumes: `statusTier` from Task 4; `Session` from `@/api/types`.
- Produces: `export function useNeedsYouTitle(sessions: Session[]): void`.

- [ ] **Step 1: Write the failing test**

Create `web/src/hooks/__tests__/useNeedsYouTitle.test.ts`:

```typescript
import { describe, it, expect, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useNeedsYouTitle } from '../useNeedsYouTitle'
import type { Session } from '@/api/types'

function s(id: string, status: string): Session {
  return { id, status } as unknown as Session
}

afterEach(() => { document.title = 'ccswitch' })

describe('useNeedsYouTitle', () => {
  it('sets a count when sessions need you', () => {
    renderHook(() => useNeedsYouTitle([s('a', 'waiting_input'), s('b', 'in_progress'), s('c', 'ready_review')]))
    expect(document.title).toBe('(2) ccswitch — needs you')
  })

  it('resets to plain title when none need you', () => {
    renderHook(() => useNeedsYouTitle([s('a', 'in_progress'), s('b', 'done')]))
    expect(document.title).toBe('ccswitch')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/hooks/__tests__/useNeedsYouTitle.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the hook**

Create `web/src/hooks/useNeedsYouTitle.ts`:

```typescript
import { useEffect } from 'react'
import { statusTier } from '@/session/statusTier'
import type { Session } from '@/api/types'

const BASE_TITLE = 'ccswitch'

/** Reflect the count of blocking ("needs you") sessions in the browser tab title,
 *  so a backgrounded tab surfaces work waiting on the user. */
export function useNeedsYouTitle(sessions: Session[]): void {
  const needsYou = sessions.filter((s) => statusTier(s.status) === 'needs-you').length
  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) ${BASE_TITLE} — needs you` : BASE_TITLE
    return () => { document.title = BASE_TITLE }
  }, [needsYou])
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/hooks/__tests__/useNeedsYouTitle.test.ts`
Expected: PASS.

- [ ] **Step 5: Wire into `Dashboard`**

In `web/src/components/Dashboard.tsx`, where `const { data: sessions = [] } = useSessions()` is in scope (~line 1027), add the import and call:

```typescript
import { useNeedsYouTitle } from '@/hooks/useNeedsYouTitle'
// ...
  useNeedsYouTitle(sessions)
```

Place the call unconditionally at the top level of the component (not inside a branch) to satisfy the rules-of-hooks.

- [ ] **Step 6: Type-check + lint + commit**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/hooks src/components/Dashboard.tsx`
Expected: clean.

```bash
git add web/src/hooks/useNeedsYouTitle.ts web/src/hooks/__tests__/useNeedsYouTitle.test.ts web/src/components/Dashboard.tsx
git commit -m "feat(web): browser tab title shows count of sessions needing you

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Full verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Backend — full suite**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q`
Expected: all green. Investigate any failure referencing `working`/`idle` status strings and update assertions to the rich vocabulary.

- [ ] **Step 2: Web — full gates (in `web/`)**

Run:
```bash
npx vitest run
npx tsc --noEmit
npx eslint .
```
Expected: all green.

- [ ] **Step 3: Grep for any surviving parallel status map**

Run: `grep -rn "SESSION_STATUS_MAP\|'working'\|\"working\"" web/src server | grep -v node_modules`
Expected: only the tolerated `working` alias inside `statusTier.ts` (and any FakeSDK internals) remain; no second status→label table. If a stray copy exists, fold it into `statusTier`.

- [ ] **Step 4: Manual smoke (optional but recommended)**

Use the `run` skill or point a browser at the dev server. Start a session, send a long prompt, and while it is working: navigate away to another session and back — the header must show "Working…", never "Ready". Trigger a permission/question — list pill pulses amber "Permission needed"/"Answer needed", the blocked session sorts to the top, and the tab title shows `(1) ccswitch — needs you`.

- [ ] **Step 5: Open the PR**

```bash
git push -u origin HEAD
gh pr create --title "Session status clarity: one source of truth, 3-tier presentation" \
  --body "$(cat <<'EOF'
## Summary
Fixes the In-Progress/Ready inconsistency and makes session state unmistakable.

- Root cause: two parallel status systems (persisted+polled list vs ephemeral WS StatusBar that was never replayed on attach). Unifies on the rich `derive_status` vocabulary end to end.
- Live status now emitted to both the StatusBar and the list on every transition; replayed on WS attach (kills the stale "Ready" mid-turn).
- Adds `waiting_question` so "Permission needed" vs "Answer needed" is distinguishable.
- One frontend source of truth (`statusTier` + presentation map) drives the list pill and the StatusBar; deletes the divergent `SESSION_STATUS_MAP`.
- Three tiers: BUSY · NEEDS YOU NOW (pulsing amber) · YOUR TURN (calm), + error. List sorts needs-you/error to top, busy to bottom. Tab title shows the needs-you count.

Spec: `docs/superpowers/specs/2026-06-22-session-status-clarity-design.md`
Plan: `docs/superpowers/plans/2026-06-22-session-status-clarity.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- Root-cause unification (single source of truth) → Tasks 2, 4, 6. ✓
- Replay on attach (the bug) → Task 3 (+ regression test). ✓
- Three tiers + error → Task 4 map; rendered in Tasks 5 (bar) & 6 (pill). ✓
- Sub-type labels "Permission needed"/"Answer needed"/"Approve plan" → Task 1 (`waiting_question`) + Task 4 labels. *(This implements the user's explicit "specific by sub-type" choice; it extends the spec's noted "out of scope" deferral — called out at handoff.)* ✓
- Calm your-turn → Task 4 (`CALM` class, no pulse). ✓
- Loud/pulsing list pill → Task 6 (`motion-safe:animate-pulse`). ✓
- Sort blocked to top → Task 7. ✓
- Browser tab title → Task 8. ✓
- Reduced motion → Task 6 `motion-safe:` + existing `WorkingIndicator`. ✓
- Edge cases: disconnect keeps last tier (StatusBar renders status independent of connectionState) ✓; replaying/no-status placeholder — see note below; sort stability → Task 7. 
- Testing (backend attach regression + frontend tier/bar/pill/sort/title) → Tasks 1–8. ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step shows full code. The only conditional instructions ("if the test file has no `makeSession` factory…") give an explicit fallback action. ✓

**3. Type consistency:** `statusTier`/`statusPresentation`/`StatusTier`/`StatusPresentation` used identically across Tasks 4–8. `current_status()`/`_emit_status()`/`queued()` consistent across Tasks 2–3. `SessionStatus` gains `waiting_question` in Task 4, consumed by Tasks 5–6 tests. ✓

**Note on the "replaying placeholder" spec item:** the spec proposed showing a quiet "…" while `replaying` is true and no status has arrived. With Task 3 emitting a status frame immediately on attach, the window is effectively eliminated, and the `FALLBACK` in `statusTier` renders a calm "Ready" rather than anything alarming. A distinct "…" placeholder is therefore deferred as unnecessary; if the brief reconnect flash proves visible in manual smoke (Task 9 Step 4), add a `replaying`-gated placeholder to `StatusBar` as a follow-up. This is a deliberate YAGNI call, flagged for the reviewer.
