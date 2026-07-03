# Dropped-turn Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the looping red-error auto-resend of a dropped turn (`[ede_diagnostic] stop_reason=null`) with a self-tuning ladder — plain resend, then `/compact`-then-resend, then an explicit paused surface — and render the in-flight recovery as a soft notice instead of a red error.

**Architecture:** The driver already classifies a `turn_dropped` `ErrorEvent` and signals the orchestrator, which resends. We (1) make the driver emit a soft `Recovering` event to the UI in place of the red `ErrorEvent` for dropped turns, (2) make the orchestrator escalate plain-resend → `/compact`+resend by resend count and pause at the cap, surfacing a clear give-up error, and (3) add the `Recovering` wire event + a muted frontend card.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React+TS (vitest, `tsc -b`) frontend.

## Global Constraints

- Backend tests: `<main-checkout>/.venv/bin/python -m pytest server/tests/…` (fresh worktree has no built env). From this worktree set `PYTHONPATH=<worktree>/src`.
- Web tests run in `web/`: `npx vitest run …`, type gate is `npx tsc -b` (NOT `tsc --noEmit`, which checks nothing here).
- A new wire event touches BOTH the backend `to_wire()` and the frontend `SESSION_EVENT_TYPES` set + reducer — shape/sequence drift is exactly what per-file runs miss, so the final task runs the FULL server suite + full vitest + `tsc -b`.
- Resend bound stays `RESEND_CAP = 3 / RESEND_WINDOW = 120.0s` (`server/sessions/recovery.py:36`). With the ladder that is: 1 plain resend + 2 `/compact`-then-resend before pausing.
- New status timestamps must use `server/time_iso.py:iso_utc`, never bare `.isoformat()` (existing `_pause_session` already complies).
- Assert orchestrator recovery behavior against a REAL `RecoveryState`, not only a fake — the recovery facade has a field/method fan-out where fakes mask omissions.
- **Scope boundary:** softening is LIVE-path only. `server/sessions/replay.py` does not reconstruct an `ErrorEvent` for a historical dropped turn, so there is no replay inconsistency to handle and no replay change in scope.

---

### Task 1: `resend_count` helper on RecoveryState

**Files:**
- Modify: `server/sessions/recovery.py:101-111` (the dropped-turn resend section)
- Test: `server/tests/test_recovery.py` (add to the existing file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RecoveryState.resend_count(session_id: str, *, now: float) -> int` — count of resends recorded within `RESEND_WINDOW` of `now` (prunes expired entries, same as the cap check). `resend_cap_reached` is refactored to delegate to it (DRY).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_recovery.py`:

```python
def test_resend_count_within_and_outside_window():
    rs = RecoveryState()
    assert rs.resend_count("s1", now=1000.0) == 0
    rs.record_resend("s1", now=1000.0)
    rs.record_resend("s1", now=1001.0)
    assert rs.resend_count("s1", now=1002.0) == 2
    # An entry older than RESEND_WINDOW (120s) is pruned.
    assert rs.resend_count("s1", now=1000.0 + 121.0) == 1


def test_resend_cap_reached_uses_count():
    rs = RecoveryState()
    for t in range(RECOVERY_RESEND_CAP):
        rs.record_resend("s1", now=1000.0 + t)
    assert rs.resend_count("s1", now=1000.0 + RECOVERY_RESEND_CAP) == RECOVERY_RESEND_CAP
    assert rs.resend_cap_reached("s1", now=1000.0 + RECOVERY_RESEND_CAP) is True
```

Ensure the imports at the top of the test file include `RecoveryState` and the cap constant. The cap constant is `RESEND_CAP` in `recovery.py`; import it as needed (alias shown above as `RECOVERY_RESEND_CAP` only for readability — use the real name `RESEND_CAP`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/ec9e5bce && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_recovery.py -k resend -v`
Expected: FAIL — `AttributeError: 'RecoveryState' object has no attribute 'resend_count'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/recovery.py`, replace the existing `resend_cap_reached` (lines ~105-111) and add `resend_count` just above it:

```python
    def resend_count(self, session_id: str, *, now: float) -> int:
        """Dropped-turn resends recorded for *session_id* within the window
        (expired entries are pruned, matching the cap check)."""
        cutoff = now - self._resend_window
        recent = [t for t in self._resends.get(session_id, ()) if t > cutoff]
        self._resends[session_id] = recent
        return len(recent)

    def resend_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already auto-resent ``resend_cap`` dropped turns
        within the window."""
        return self.resend_count(session_id, now=now) >= self._resend_cap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_recovery.py -k resend -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/recovery.py server/tests/test_recovery.py
git commit -m "feat(recovery): resend_count helper; resend_cap_reached delegates to it"
```

---

### Task 2: `Recovering` wire event

**Files:**
- Modify: `server/sessions/events.py:164` (add after `ErrorEvent`, before `ReplayComplete`)
- Test: `server/tests/test_events.py` (add; if the file does not exist, create it with the import shown)

**Interfaces:**
- Consumes: nothing.
- Produces: `events.Recovering(turn_id: str | None, message: str)` dataclass with
  `to_wire() -> {"type": "recovering", "turnId": ..., "message": ..., "ts": ...}`. Used by the driver (Task 3).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_events.py`:

```python
from server.sessions import events as ev


def test_recovering_to_wire():
    e = ev.Recovering(turn_id="t1", message="Recovering the dropped turn…")
    w = e.to_wire()
    assert w["type"] == "recovering"
    assert w["turnId"] == "t1"
    assert w["message"] == "Recovering the dropped turn…"
    assert "ts" in w
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_events.py::test_recovering_to_wire -v`
Expected: FAIL — `AttributeError: module 'server.sessions.events' has no attribute 'Recovering'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/events.py`, add immediately after the `ErrorEvent` dataclass (after line 162):

```python
@dataclass
class Recovering:
    """A turn dropped before the model produced any output and is being auto-resent.
    Self-healing — surfaced to the UI as a soft notice (NOT a red error). The
    orchestrator drives the actual resend off the TurnFailure signal."""
    turn_id: str | None; message: str
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "recovering", "turnId": self.turn_id,
                               "message": self.message, "ts": self.ts}
```

(`field` and `_now_ms` are already imported/defined in this module — `ErrorEvent` uses both.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_events.py::test_recovering_to_wire -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/tests/test_events.py
git commit -m "feat(events): add Recovering wire event (soft dropped-turn notice)"
```

---

### Task 3: Driver emits `Recovering` for dropped turns; adds `emit_error`

**Files:**
- Modify: `server/sessions/driver.py:491-508` (`_maybe_signal_recovery` returns the failure) and `server/sessions/driver.py:548-553` (the `_drain` ErrorEvent branch); add an `emit_error` method.
- Test: `server/tests/test_driver.py` (add to the existing file)

**Interfaces:**
- Consumes: `events.Recovering` (Task 2); `classify` (existing, `failure.py`).
- Produces:
  - `_maybe_signal_recovery(code, message) -> failure.TurnFailure` (was implicit `None`).
  - `SessionDriver.emit_error(code: str, message: str) -> None` — puts an `ErrorEvent` (anchored to the current turn) on the driver's out-queue. Consumed by the orchestrator give-up branch (Task 4).
  - Behavior: a `turn_dropped` `ErrorEvent` in `_drain` is forwarded to the UI as a `Recovering` event, NOT the red `ErrorEvent`; recovery signalling is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_driver.py`. Follow the existing driver-test harness in that file for constructing a `SessionDriver` with a fake client/factory and draining a scripted message stream; the assertion is what matters:

```python
@pytest.mark.asyncio
async def test_dropped_turn_emits_recovering_not_error(dropped_turn_driver):
    # dropped_turn_driver: a SessionDriver whose fake client yields a ResultMessage
    # that the translator renders as ErrorEvent(code="execution_error",
    # raw_message="[ede_diagnostic] ... stop_reason=null"). Build it mirroring the
    # existing ede/error driver tests in this file.
    drv = dropped_turn_driver
    await drv._drain()
    out = drain_queue(drv._out)  # existing helper pattern in this test module
    kinds = [type(e).__name__ for e in out]
    assert "Recovering" in kinds
    assert "ErrorEvent" not in kinds          # red error suppressed for the dropped turn


@pytest.mark.asyncio
async def test_emit_error_puts_errorevent(basic_driver):
    drv = basic_driver
    await drv.emit_error("turn_dropped_cap", "gave up")
    out = drain_queue(drv._out)
    assert any(isinstance(e, ev.ErrorEvent) and e.code == "turn_dropped_cap" for e in out)
```

If the test file has no `drain_queue`/fixture exactly named, reuse whatever the existing error/ede driver tests use to build a driver and read `_out`; keep the assertions identical.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_driver.py -k "recovering or emit_error" -v`
Expected: FAIL — `Recovering` not emitted / `emit_error` undefined.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/driver.py`, change `_maybe_signal_recovery` to return the failure:

```python
    async def _maybe_signal_recovery(self, code: str, message: str) -> "TurnFailure":
        """Classify a turn-failure surface; if recoverable, emit TurnFailure and notify
        the orchestrator. Returns the classification so the caller can adapt the UI
        surface (e.g. soften a dropped turn). Retry-After comes from the error text,
        falling back to a RateLimitEvent reset observed this turn (resets_at - now)."""
        failure = classify(code, message)
        if not failure.is_recoverable:
            return failure
        retry_after = failure.retry_after
        if retry_after is None and self._last_rate_reset is not None:
            retry_after = max(0.0, float(self._last_rate_reset) - self._clock())
        await self._out.put(ev.TurnFailure(
            kind=failure.kind, account=self.account, retry_after=retry_after))
        if self._on_recoverable_failure is not None:
            try:
                await self._on_recoverable_failure(self.session_id, failure.kind, retry_after)
            except Exception:
                log.warning("driver.on_recoverable_failure failed session=%s",
                            self.session_id, exc_info=True)
        return failure
```

Add the import for the return type at the top of the file if not present: `from .failure import classify, TurnFailure` (the module already imports `classify` at line 11 — extend it).

Add a module-level constant near the other driver constants:

```python
_RECOVERING_MSG = "Recovering the dropped turn…"
```

Add the `emit_error` method (near the other small `_out`-emitting helpers, e.g. after `_maybe_signal_recovery`):

```python
    async def emit_error(self, code: str, message: str) -> None:
        """Surface a red error card anchored to the current turn. Used by the
        orchestrator to explain an exhausted auto-recovery (the give-up surface)."""
        await self._out.put(ev.ErrorEvent(code, message, turn_id=self._turn()))
```

In `_drain`, replace the existing ErrorEvent branch (lines 548-549) so a dropped turn is softened. The current code is:

```python
                    elif isinstance(e, ev.ErrorEvent):
                        await self._maybe_signal_recovery(e.code, e.raw_message or e.message)
```

Replace with:

```python
                    elif isinstance(e, ev.ErrorEvent):
                        failure = await self._maybe_signal_recovery(
                            e.code, e.raw_message or e.message)
                        if failure.kind == "turn_dropped":
                            # Self-healing auto-resend is in flight (signalled above).
                            # Show a soft "recovering" notice instead of the alarming
                            # red error; the orchestrator pauses with a red surface only
                            # if the resend cap is hit.
                            self._note_progress(e)
                            await self._out.put(ev.Recovering(
                                turn_id=e.turn_id, message=_RECOVERING_MSG))
                            continue
```

The trailing `self._note_progress(e)` + `await self._out.put(e)` at the bottom of the loop (lines 552-553) still run for every non-dropped error, preserving today's red-error behavior. The `continue` skips them for the dropped case (we already noted progress and queued the `Recovering` event).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_driver.py -k "recovering or emit_error" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole driver + translator suite (sequence asserts)**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_driver.py server/tests/test_translator.py -v`
Expected: PASS. If a `type(e).__name__` sequence assertion now fails for a dropped-turn fixture, update it to expect `Recovering` where it previously expected `ErrorEvent` — that is the intended change, not a regression.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver.py
git commit -m "feat(driver): soften dropped-turn to a Recovering event; add emit_error"
```

---

### Task 4: Orchestrator escalation ladder + pause-at-cap

**Files:**
- Modify: `server/sessions/orchestrator.py:2334-2342` (`_recover_session`, `turn_dropped` branch) and `server/sessions/orchestrator.py:2395-2401` (`_replay_same_account` gains `precompact`).
- Test: `server/tests/test_orchestrator_recovery.py` (add; if absent, place with the other orchestrator recovery tests — grep `test_.*recover` under `server/tests/` to find the right file).

**Interfaces:**
- Consumes: `RecoveryState.resend_count` (Task 1); `SessionDriver.send_prompt` (existing), `SessionDriver.emit_error` (Task 3).
- Produces: ladder behavior — 1st drop → plain resend; 2nd/3rd → `/compact` then resend; cap → `emit_error` + `_pause_session(reason="turn_dropped_cap")`.

- [ ] **Step 1: Write the failing test**

Add to the orchestrator recovery test module. Use the module's existing orchestrator+fake-driver harness; capture `send_prompt` calls on the fake driver. Skeleton:

```python
@pytest.mark.asyncio
async def test_dropped_turn_ladder(orch_with_session):
    orch, record, driver = orch_with_session       # fake driver records send_prompt calls
    orch._last_prompt[record.id] = "do the thing"

    # 1st drop → plain resend, no /compact
    await orch._recover_session(record, "turn_dropped", None)
    assert driver.sent == ["do the thing"]

    # 2nd drop → /compact THEN resend
    await orch._recover_session(record, "turn_dropped", None)
    assert driver.sent[-2:] == ["/compact", "do the thing"]

    # 3rd drop → /compact THEN resend again
    await orch._recover_session(record, "turn_dropped", None)
    assert driver.sent[-2:] == ["/compact", "do the thing"]

    # 4th drop → cap reached: no further resend, give-up error + pause
    await orch._recover_session(record, "turn_dropped", None)
    assert driver.sent[-2:] == ["/compact", "do the thing"]   # unchanged
    assert driver.errors and driver.errors[-1][0] == "turn_dropped_cap"
    assert record.status == "needs_attention"
```

Build the fake driver with `sent: list[str]` appended by `send_prompt`, and `errors: list[tuple[str,str]]` appended by `emit_error`. Use a REAL `RecoveryState` on the orchestrator (per Global Constraints), and a monotonically-increasing fake clock so all four drops fall inside the 120s window.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -k ladder -v`
Expected: FAIL — current code resends with no `/compact` and `return`s silently at the cap (no `emit_error`, status not `needs_attention`).

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, replace the `turn_dropped` branch (lines 2334-2342) of `_recover_session`:

```python
        if kind == "turn_dropped":
            # A turn that dropped before the model committed any continuation. Escalate:
            # 1st drop → plain resend (cheap; absorbs a transient drop). Repeat drops in
            # the window → /compact first (the context is likely full and re-dropping the
            # resend). At the cap, stop and surface an explicit give-up — not silence.
            if self._recovery.resend_cap_reached(record.id, now=now):
                driver = self._drivers.get(record.id)
                if driver is not None:
                    await driver.emit_error("turn_dropped_cap", _TURN_DROPPED_GIVEUP)
                await self._pause_session(record, reason="turn_dropped_cap")
                return
            precompact = self._recovery.resend_count(record.id, now=now) >= 1
            self._recovery.record_resend(record.id, now=now)
            await self._replay_same_account(record, 0.0, precompact=precompact)
            return
```

Add a module-level constant near the other orchestrator message/text constants:

```python
_TURN_DROPPED_GIVEUP = ("Auto-recovery gave up after repeated empty turns — the context "
                        "may be full. Send a message to retry, or /compact to free space.")
```

Update `_replay_same_account` (lines 2395-2401) to optionally compact first:

```python
    async def _replay_same_account(self, record: SessionRecord, retry_after: float,
                                   *, precompact: bool = False) -> None:
        """Wait out a short Retry-After, then re-send the last prompt on the same account.
        When *precompact* is set, send /compact first; the driver's FIFO prompt queue runs
        the resend immediately after the compaction turn completes (no manual await)."""
        await asyncio.sleep(max(0.0, retry_after))
        driver = self._drivers.get(record.id)
        last = self._last_prompt.get(record.id)
        if driver is None or not last:
            return
        if precompact:
            await driver.send_prompt("/compact")
        await driver.send_prompt(last)
```

Note: the second `send_prompt(last)` is enqueued behind `/compact` by the driver's existing FIFO queue (`driver.py:431`), so ordering is automatic.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd … && PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -k ladder -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_recovery.py
git commit -m "feat(orchestrator): dropped-turn ladder — compact-then-resend, pause at cap"
```

---

### Task 5: Frontend `recovering` event + reducer block

**Files:**
- Modify: `web/src/session/events.ts:22` (add `EvRecovering`, extend `SessionEvent` + `SESSION_EVENT_TYPES`), `web/src/session/reducer.ts:18` (Block union) and `:643` (new `case 'recovering'` after `case 'error'`).
- Test: `web/src/session/reducer.test.ts` (add)

**Interfaces:**
- Consumes: wire `{type:'recovering', turnId, message, ts?}` from Task 2's backend event.
- Produces: a `{ kind: 'recovering'; id; message; ts? }` block appended to the turn; `state.error` stays `null`.

- [ ] **Step 1: Write the failing test**

Add to `web/src/session/reducer.test.ts` (mirror an existing `case 'error'` test for setup):

```ts
it('recovering event appends a soft block and does NOT raise the error banner', () => {
  let s = initialState()
  s = reduce(s, { type: 'turnStart', turnId: 't1' } as SessionEvent)
  s = reduce(s, { type: 'recovering', turnId: 't1', message: 'Recovering the dropped turn…' } as SessionEvent)
  const turn = s.turns.find(t => t.id === 't1')!
  expect(turn.blocks.some(b => b.kind === 'recovering')).toBe(true)
  expect(s.error).toBeNull()
})
```

(Use whatever `initialState`/`reduce` import names the file already uses.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/session/reducer.test.ts -t recovering`
Expected: FAIL — `recovering` not handled / type error on the cast.

- [ ] **Step 3: Write minimal implementation**

In `web/src/session/events.ts`, add the interface after `EvError` (line 22):

```ts
export interface EvRecovering    { type: 'recovering';    turnId: string; message: string; ts?: number }
```

Add `EvRecovering` to the `SessionEvent` union (line 31-35) and `'recovering'` to `SESSION_EVENT_TYPES` (line 45-49):

```ts
  | EvSuggestions | EvCondensing | EvContextUsage | EvRecovering
```
```ts
  'suggestions','condensing','contextUsage','recovering',
```

In `web/src/session/reducer.ts`, add the Block variant (after line 18):

```ts
  | { kind: 'recovering'; id: string; message: string; ts?: number }
```

Add the reducer case immediately after the `case 'error'` block (after line 643):

```ts
    // ── recovering ──────────────────────────────────────────────────────────────
    case 'recovering': {
      // A dropped turn is being auto-resent. Append a soft inline block; deliberately
      // do NOT raise state.error — this is self-healing, not a failure. If recovery is
      // exhausted the backend sends a real `error` event (the red give-up surface).
      const [withTurn, idx] = ensureAssistantTurn(state.turns, ev.turnId)
      const turn = withTurn[idx]
      const block: Block = {
        kind: 'recovering', id: `${ev.turnId}:rec:${turn.blocks.length}`,
        message: ev.message, ts: ev.ts,
      }
      const turns = replaceTurn(withTurn, idx, (t) => ({ ...t, blocks: [...t.blocks, block] }))
      return { ...state, turns }
    }
```

- [ ] **Step 4: Run test + typecheck**

Run: `cd web && npx vitest run src/session/reducer.test.ts -t recovering && npx tsc -b`
Expected: test PASS; `tsc -b` clean. (`tsc -b` will flag any block-rendering switch that is not exhaustive — fixed in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.test.ts
git commit -m "feat(web): recovering wire event + soft reducer block (no error banner)"
```

---

### Task 6: Frontend `RecoveringCard` + render in Turn

**Files:**
- Create: `web/src/components/session/RecoveringCard.tsx`
- Modify: `web/src/components/session/Turn.tsx:10` (import) and `:94-95` (add `case 'recovering'` to the block switch)
- Test: `web/src/components/session/__tests__/` (add a small render test next to existing session-component tests)

**Interfaces:**
- Consumes: the `{ kind: 'recovering'; message }` block (Task 5).
- Produces: a muted, non-red inline card. No new exported types.

- [ ] **Step 1: Write the failing test**

Add `web/src/components/session/__tests__/RecoveringCard.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { RecoveringCard } from '../RecoveringCard'

it('renders the recovering message without alert role or red styling', () => {
  render(<RecoveringCard message="Recovering the dropped turn…" />)
  expect(screen.getByText('Recovering the dropped turn…')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).toBeNull()   // NOT an error surface
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/RecoveringCard.test.tsx`
Expected: FAIL — module `../RecoveringCard` not found.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/components/session/RecoveringCard.tsx` (mutes the `ErrorCard` styling — no red, no `role="alert"`, a quiet status tone):

```tsx
import { cn } from '@/lib/utils'

interface Props {
  message: string
}

/**
 * Inline transcript card for a self-healing dropped-turn auto-resend. Deliberately
 * muted (no red, no alert role) — the turn produced no output and is being resent;
 * it is recovering, not failed. Contrast ErrorCard, the red give-up surface used
 * only when auto-recovery is exhausted.
 */
export function RecoveringCard({ message }: Props) {
  return (
    <div
      className={cn(
        'my-1 rounded-md px-3 py-2 text-sm',
        'bg-amber-50 dark:bg-amber-950/30 text-amber-800 dark:text-amber-300/90',
        'border border-amber-200/70 dark:border-amber-900/60',
      )}
    >
      <span className="font-medium">Recovering</span>
      <div className="mt-0.5 whitespace-pre-wrap break-words">{message}</div>
    </div>
  )
}
```

In `web/src/components/session/Turn.tsx`, add the import next to `ErrorCard` (line 10):

```tsx
import { RecoveringCard } from './RecoveringCard'
```

Add the case to the block switch, next to `case 'error'` (after line 95):

```tsx
          case 'recovering':
            return <BlockRow key={block.id} ts={rowTs}><RecoveringCard message={block.message} /></BlockRow>
```

- [ ] **Step 4: Run test + typecheck**

Run: `cd web && npx vitest run src/components/session/__tests__/RecoveringCard.test.tsx && npx tsc -b`
Expected: test PASS; `tsc -b` clean (the block switch is now exhaustive).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/RecoveringCard.tsx web/src/components/session/Turn.tsx web/src/components/session/__tests__/RecoveringCard.test.tsx
git commit -m "feat(web): RecoveringCard — muted inline surface for dropped-turn recovery"
```

---

### Task 7: Full-suite verification + lint

**Files:** none (verification only).

- [ ] **Step 1: Backend lint + full server suite**

Run:
```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/ec9e5bce
PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m ruff check server/
PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m mypy server/sessions/driver.py server/sessions/orchestrator.py server/sessions/recovery.py server/sessions/events.py
PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/ -q
```
Expected: ruff + mypy clean; pytest green except the known environmental failures documented in project memory (judge by diffing the failing node-id set against a baseline worktree, not raw counts).

- [ ] **Step 2: Web full suite + real typecheck + lint**

Run:
```bash
cd web && npx vitest run && npx tsc -b && npx eslint . --max-warnings 0
```
Expected: all green. eslint `--max-warnings 0` is the CI gate; fix any react-hooks warnings.

- [ ] **Step 3: Manual smoke (optional but recommended)**

If a live/dev session is reachable, drive a dropped turn (or unit-confirm) and verify: in-flight shows the amber "Recovering…" card with no red banner; after 3 resends in 120s the session flips to `needs_attention` with the give-up error message.

- [ ] **Step 4: Finish the branch**

Follow CLAUDE.md completion workflow: branch off `origin/master`, push, open a NON-draft PR with `Fixes`/`Refs` as appropriate, mark ready, arm `--squash --auto`.

---

## Self-Review

**Spec coverage:**
- Behavior ladder (plain → compact → pause) → Task 4. ✓
- `/compact` via `send_prompt`, FIFO-ordered before resend → Task 4 (`_replay_same_account`). ✓
- Driver splits dual-duty ErrorEvent → soft Recovering → Task 3. ✓
- Recovering wire event (backend + frontend type/registry) → Tasks 2 + 5. ✓
- Reducer adds block, does NOT raise `state.error` → Task 5. ✓
- Muted RecoveringCard + Turn render → Task 6. ✓
- Give-up = explicit needs_attention + clear "context may be full" message → Task 4 (`emit_error` + `_pause_session`). ✓
- `resend_count` helper → Task 1. ✓
- Full-suite/`tsc -b`/lint gate → Task 7. ✓
- Replay out of scope (no historical ErrorEvent reconstructed) → stated in Global Constraints. ✓

**Placeholder scan:** No "TBD"/"add error handling"-style gaps; every code step shows real code. The only deferred detail in the spec (`/compact`-then-resend ordering) is resolved concretely in Task 4 via the driver FIFO. ✓

**Type consistency:** `Recovering(turn_id, message)` (Task 2) ↔ driver `ev.Recovering(turn_id=…, message=…)` (Task 3) ↔ wire `{type:'recovering', turnId, message, ts}` ↔ `EvRecovering` (Task 5) ↔ Block `{kind:'recovering', message}` (Task 5) ↔ `RecoveringCard({message})` (Task 6) — aligned. `resend_count(session_id, *, now)` defined Task 1, consumed Task 4. `emit_error(code, message)` defined Task 3, consumed Task 4. `_replay_same_account(record, retry_after, *, precompact=False)` defined and consumed in Task 4. ✓
