# Transcript Gap-Healing (PR 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make lost/dropped live transcript events *detectable* and *self-healing*, and stop cross-session event contamination, without changing the existing replay machinery.

**Architecture:** The server stamps every **live** transcript event (in `_forward_live`) with a per-session monotonic `seq` and the owning `sessionId`, and hardens the outbound pipeline so one bad event can't silently freeze the stream. The client filters events by active session (defense in depth), detects a `seq` gap, and triggers a rate-limited re-attach — which runs the existing `replayReset → JSONL replay → replayComplete` path to rebuild the transcript cleanly. All decision logic is extracted into pure, unit-tested helpers; the socket wiring stays thin.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React + TypeScript (vitest) frontend.

## Global Constraints

- Account = `CLAUDE_CONFIG_DIR`; never fork/patch the Claude binary (constitution I). This PR touches only ccswitch's own transport, not the SDK.
- Single source of truth / no parallel implementations (constitution VI): `seq`/`sessionId` are assigned at exactly one point (`_forward_live`); gap logic lives in one pure helper (`web/src/ws/liveBatch.ts`).
- Add new SQLite columns nullable only — **N/A here** (no schema changes in PR 1).
- Server tests: `<main-checkout>/.venv/bin/python -m pytest server/tests/…` in a fresh worktree. Web tests run in `web/`: `vitest run`, `tsc --noEmit`, `eslint`.
- Wire frames are already-redacted dicts; `seq` (int) and `sessionId` (str) survive `redact_event` (verified: `server/audit.py:78` → `redact_detail` preserves structure).
- The client singleton socket exposes `this._activeSessionId`; a gap heal re-sends `{type:'attach', sessionId}` (same frame `connect()`'s `onopen` uses).

---

### Task 1: Server — per-session `seq` + `sessionId` on live events, hardened outbound pipeline

**Files:**
- Modify: `server/bridge/ws.py` — `_forward_live` (`:59-79`), `_batcher` (`:161-178`); add module-level `_redact_buffer`.
- Test: `server/tests/test_ws_gap_healing.py` (create)

**Interfaces:**
- Consumes: `driver.events()` (async generator yielding event objects with `.to_wire() -> dict`), module-level `redact_event` (`server/audit.py`).
- Produces:
  - `_forward_live(driver, out_q, session_id)` now enqueues wire dicts each carrying `"sessionId": str` and `"seq": int` (per-session monotonic from 0; a `to_wire()` failure advances `seq` so the client observes a gap).
  - `_redact_buffer(buf: list[dict]) -> list[dict]` — redacts each event, dropping (not raising on) any event whose redaction throws.

- [ ] **Step 1: Write the failing test for seq/sessionId stamping**

```python
# server/tests/test_ws_gap_healing.py
import asyncio
import pytest
from server.bridge.ws import _forward_live, _redact_buffer


class _FakeEvent:
    def __init__(self, wire): self._wire = wire
    def to_wire(self): return dict(self._wire)


class _BoomEvent:
    def to_wire(self): raise RuntimeError("bad event")


class _FakeDriver:
    def __init__(self, events): self._events = events
    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_forward_live_stamps_seq_and_session_id():
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _FakeEvent({"type": "textDelta", "text": "b"})])
    await _forward_live(driver, out_q, "sess-1")
    first = out_q.get_nowait()
    second = out_q.get_nowait()
    assert first["sessionId"] == "sess-1" and first["seq"] == 0
    assert second["sessionId"] == "sess-1" and second["seq"] == 1


@pytest.mark.asyncio
async def test_forward_live_gap_on_to_wire_failure():
    """A single bad event must not kill the stream, and must leave a seq hole
    so the client self-heals."""
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _BoomEvent(),
                          _FakeEvent({"type": "textDelta", "text": "c"})])
    await _forward_live(driver, out_q, "sess-1")
    got = [out_q.get_nowait(), out_q.get_nowait()]
    assert out_q.empty()
    assert [e["seq"] for e in got] == [0, 2]  # seq 1 was consumed by the failed event
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_ws_gap_healing.py -v` (use the main checkout's `.venv` in a fresh worktree)
Expected: FAIL — `ImportError: cannot import name '_redact_buffer'` (and/or KeyError on `seq`).

- [ ] **Step 3: Rewrite `_forward_live` to stamp seq/sessionId and survive bad events**

Replace the body of `_forward_live` (`server/bridge/ws.py:59-79`):

```python
async def _forward_live(driver, out_q: "asyncio.Queue[dict]", session_id: str) -> None:
    """Forward a driver's live events into *out_q* as wire dicts.

    Each dict is stamped with the owning ``sessionId`` and a per-session monotonic
    ``seq`` so the client can (a) discard events from a session it is no longer
    viewing and (b) detect a hole and self-heal via re-attach. A single event that
    fails to serialize must not silence the whole stream: skip it but still advance
    ``seq`` so the gap is visible. A crash in the generator becomes an ``error`` frame.
    """
    with bind_session(session_id):
        seq = 0
        try:
            async for e in driver.events():
                try:
                    wire = e.to_wire()
                except Exception:
                    log.exception("ws.to_wire session=%s", session_id)
                    seq += 1  # consume a seq so the client sees a gap and re-attaches
                    continue
                wire["sessionId"] = session_id
                wire["seq"] = seq
                seq += 1
                out_q.put_nowait(wire)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("ws.live_stream session=%s", session_id)
            detail = str(exc).strip() or "no detail"
            out_q.put_nowait(ErrorEvent(
                "live_stream_failed",
                f"The live session stream stopped unexpectedly. {type(exc).__name__}: {detail[:500]}",
                turn_id=None,
            ).to_wire())
```

- [ ] **Step 4: Add `_redact_buffer` and harden `_batcher`**

Add a module-level helper near `_forward_live` (after it):

```python
def _redact_buffer(buf: list[dict]) -> list[dict]:
    """Redact each outbound event; drop any event whose redaction raises instead of
    letting one bad event kill the batcher task (which would freeze the socket).
    A dropped event leaves a seq hole, so the client self-heals."""
    out: list[dict] = []
    for e in buf:
        try:
            out.append(redact_event(e))
        except Exception:
            log.exception("ws.redact: dropping unredactable event type=%s", e.get("type"))
    return out
```

Then in `_batcher` (`server/bridge/ws.py:174-176`) replace:

```python
                    # Redact each event dict before transmission.
                    redacted_buf = [redact_event(e) for e in buf]
                    await _send({"type": "batch", "events": redacted_buf})
```

with:

```python
                    await _send({"type": "batch", "events": _redact_buffer(buf)})
```

- [ ] **Step 5: Add the `_redact_buffer` resilience test**

```python
def test_redact_buffer_drops_unredactable_without_raising(monkeypatch):
    import server.bridge.ws as wsmod
    good = {"type": "textDelta", "text": "ok", "seq": 0}
    bad = {"type": "textDelta", "text": "boom", "seq": 1}

    def fake_redact(e):
        if e["text"] == "boom":
            raise RuntimeError("redact blew up")
        return e

    monkeypatch.setattr(wsmod, "redact_event", fake_redact)
    out = wsmod._redact_buffer([good, bad])
    assert out == [good]  # bad dropped, no exception, frame still flows
```

- [ ] **Step 6: Run the full test file to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_ws_gap_healing.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Run the broader ws suite for regressions**

Run: `.venv/bin/python -m pytest server/tests/ -k "ws or replay or forward" -q`
Expected: PASS (no regressions in existing WS/replay tests).

- [ ] **Step 8: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_gap_healing.py
git commit -m "feat(ws): stamp live events with seq+sessionId and harden outbound pipeline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Client — pure live-batch handler (session filter + gap detection)

**Files:**
- Create: `web/src/ws/liveBatch.ts`
- Test: `web/src/ws/liveBatch.test.ts`

**Interfaces:**
- Consumes: `SessionEvent` (`web/src/session/events.ts`). Live events now optionally carry `seq?: number` and `sessionId?: string` (present on live-stream events, absent on replay/lifecycle events).
- Produces:
  - `type GapState = { lastSeq: Map<string, number> }`
  - `createGapState(): GapState`
  - `resetSeq(state: GapState, sessionId: string): void` — clears the baseline for a session (called on `replayReset`).
  - `handleLiveBatch(events: SessionEvent[], ctx: { activeSessionId: string | null; state: GapState }): { dispatch: SessionEvent[]; needsHeal: boolean }` — filters out events for non-active sessions, detects a `seq` gap for the active session, and returns the events to dispatch plus whether a heal (re-attach) is needed.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/ws/liveBatch.test.ts
import { describe, it, expect } from 'vitest'
import { createGapState, resetSeq, handleLiveBatch } from './liveBatch'
import type { SessionEvent } from '@/session/events'

const ev = (over: Record<string, unknown>): SessionEvent =>
  ({ type: 'textDelta', turnId: 't1', text: 'x', ...over }) as unknown as SessionEvent

describe('handleLiveBatch', () => {
  it('dispatches contiguous events for the active session with no heal', () => {
    const state = createGapState()
    const r = handleLiveBatch(
      [ev({ seq: 0, sessionId: 's1' }), ev({ seq: 1, sessionId: 's1' })],
      { activeSessionId: 's1', state },
    )
    expect(r.dispatch).toHaveLength(2)
    expect(r.needsHeal).toBe(false)
  })

  it('flags a heal when a seq is missing', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 2, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(true)
  })

  it('drops events belonging to a non-active session', () => {
    const state = createGapState()
    const r = handleLiveBatch(
      [ev({ seq: 0, sessionId: 's2' })],
      { activeSessionId: 's1', state },
    )
    expect(r.dispatch).toHaveLength(0)
    expect(r.needsHeal).toBe(false)   // another session's gap must not heal ours
  })

  it('re-baselines after resetSeq (reconnect replay) without a false gap', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 5, sessionId: 's1' })], { activeSessionId: 's1', state })
    resetSeq(state, 's1')
    const r = handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(false)
  })

  it('passes through events without seq/sessionId (replay/lifecycle) untouched', () => {
    const state = createGapState()
    const replayReset = { type: 'replayReset' } as SessionEvent
    const r = handleLiveBatch([replayReset], { activeSessionId: 's1', state })
    expect(r.dispatch).toEqual([replayReset])
    expect(r.needsHeal).toBe(false)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/ws/liveBatch.test.ts`
Expected: FAIL — `Cannot find module './liveBatch'`.

- [ ] **Step 3: Implement `liveBatch.ts`**

```typescript
// web/src/ws/liveBatch.ts
import type { SessionEvent } from '@/session/events'

/** Per-session last-applied live sequence number, for gap detection. */
export interface GapState {
  lastSeq: Map<string, number>
}

export function createGapState(): GapState {
  return { lastSeq: new Map() }
}

/** Clear the seq baseline for a session — called on replayReset so the next live
 *  event re-baselines instead of registering a false gap. */
export function resetSeq(state: GapState, sessionId: string): void {
  state.lastSeq.delete(sessionId)
}

interface WireMeta { seq?: number; sessionId?: string }

/**
 * Filter a live batch to the active session and detect a sequence gap.
 *
 * Events without a `seq` (replay content, lifecycle frames) are always dispatched
 * and never affect gap tracking. Events tagged with a `sessionId` other than the
 * active one are dropped (a single socket can interleave multiple sessions).
 */
export function handleLiveBatch(
  events: SessionEvent[],
  ctx: { activeSessionId: string | null; state: GapState },
): { dispatch: SessionEvent[]; needsHeal: boolean } {
  const { activeSessionId, state } = ctx
  const dispatch: SessionEvent[] = []
  let needsHeal = false

  for (const e of events) {
    const meta = e as SessionEvent & WireMeta
    // Drop events explicitly owned by a different session.
    if (meta.sessionId && activeSessionId && meta.sessionId !== activeSessionId) continue

    if (typeof meta.seq === 'number' && meta.sessionId) {
      const prev = state.lastSeq.get(meta.sessionId)
      if (prev !== undefined && meta.seq !== prev + 1) needsHeal = true
      state.lastSeq.set(meta.sessionId, meta.seq)
    }
    dispatch.push(e)
  }

  return { dispatch, needsHeal }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/ws/liveBatch.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Typecheck**

Run (in `web/`): `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/ws/liveBatch.ts web/src/ws/liveBatch.test.ts
git commit -m "feat(ws): pure live-batch handler for session filter + gap detection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Client — wire gap-healing into the socket (filter, reset baseline, rate-limited re-attach)

**Files:**
- Modify: `web/src/ws/client.ts` — `_handleInbound` `'batch'` case (`:327-331`), add gap state + heal method; import from `liveBatch.ts`.
- Test: `web/src/ws/client.gapHealing.test.ts` (create)

**Interfaces:**
- Consumes: `createGapState`, `resetSeq`, `handleLiveBatch` (Task 2); `this._activeSessionId`, `this._send` (existing).
- Produces: on a detected gap, the socket sends a rate-limited `{type:'attach', sessionId}` for the active session (heal), which triggers the server's existing `replayReset → replay → replayComplete`. `replayReset` in an inbound batch resets the seq baseline.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/ws/client.gapHealing.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SessionSocket } from './client'

// Build a socket whose _send is spied, with an active session, and drive inbound
// frames through the (test-only) inbound hook.
function makeSocket() {
  const s = new SessionSocket('/ws') as unknown as {
    _activeSessionId: string | null
    _send: (m: object) => boolean
    _handleInbound: (m: unknown) => void
  }
  s._activeSessionId = 's1'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent }
}

describe('SessionSocket gap healing', () => {
  beforeEach(() => vi.useRealTimers())

  it('re-attaches once when a live seq gap is seen', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'c', seq: 2, sessionId: 's1' }] })
    const attaches = sent.filter((m) => (m as { type: string }).type === 'attach')
    expect(attaches).toHaveLength(1)
    expect(attaches[0]).toMatchObject({ type: 'attach', sessionId: 's1' })
  })

  it('does not re-attach for contiguous events', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'b', seq: 1, sessionId: 's1' }] })
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
  })

  it('resets the baseline on replayReset so post-reconnect replay does not false-heal', () => {
    const { s, sent } = makeSocket()
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 9, sessionId: 's1' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'replayReset' }] })
    s._handleInbound({ type: 'batch', events: [{ type: 'textDelta', turnId: 't', text: 'a', seq: 0, sessionId: 's1' }] })
    expect(sent.filter((m) => (m as { type: string }).type === 'attach')).toHaveLength(0)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.gapHealing.test.ts`
Expected: FAIL — no `attach` frame sent (gap logic not wired yet).

- [ ] **Step 3: Add gap state, heal method, and imports to `client.ts`**

Add the import near the other imports (top of `web/src/ws/client.ts`):

```typescript
import { createGapState, resetSeq, handleLiveBatch, type GapState } from './liveBatch'
```

Add fields to the `SessionSocket` class (near `_pendingPrompts`, `:82`):

```typescript
  private _gap: GapState = createGapState()
  private _lastHealAt = 0
  private static readonly HEAL_COOLDOWN_MS = 2_000
```

Add a private heal method (near `_scheduleReconnect`):

```typescript
  /** Self-heal a detected live-stream gap by re-attaching the active session, which
   *  triggers the server's replayReset → replay → replayComplete rebuild. Rate-limited
   *  so a persistently gappy stream cannot storm the server with re-attaches. */
  private _healActiveSession() {
    const sid = this._activeSessionId
    if (!sid) return
    const now = Date.now()
    if (now - this._lastHealAt < SessionSocket.HEAL_COOLDOWN_MS) return
    this._lastHealAt = now
    clientLogger.warn('ws gap detected — re-attaching to heal transcript', { sessionId: sid })
    this._send({ type: 'attach', sessionId: sid })
  }
```

- [ ] **Step 4: Rewrite the `'batch'` case in `_handleInbound`**

Replace (`web/src/ws/client.ts:327-331`):

```typescript
      case 'batch':
        for (const e of msg.events) {
          this._emit('sessionEvent', e)
        }
        break
```

with:

```typescript
      case 'batch': {
        // A replayReset (reconnect replay) re-baselines the active session's seq.
        for (const e of msg.events) {
          if ((e as { type: string }).type === 'replayReset' && this._activeSessionId) {
            resetSeq(this._gap, this._activeSessionId)
          }
        }
        const { dispatch, needsHeal } = handleLiveBatch(msg.events, {
          activeSessionId: this._activeSessionId,
          state: this._gap,
        })
        for (const e of dispatch) this._emit('sessionEvent', e)
        if (needsHeal) this._healActiveSession()
        break
      }
```

- [ ] **Step 5: Run the gap-healing test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/client.gapHealing.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Full web verification (no regressions)**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src/ws/`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.gapHealing.test.ts
git commit -m "feat(ws): self-heal transcript gaps via rate-limited re-attach

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (PR 1 slice of the design doc):**
- Server assigns monotonic per-session `seq` at a single emission point → Task 1 (`_forward_live`). ✓
- Wire batch carries `sessionId` + `seq` → Task 1. ✓
- Client establishes baseline from first live event; contiguity thereafter → Task 2 (`handleLiveBatch`, `prev === undefined` skips first). ✓
- Gap → re-attach (existing replay heals) → Task 3 (`_healActiveSession`). ✓
- `sessionId` filter (defense in depth, switch contamination) → Tasks 2 + 3. ✓
- Debounce/rate-limit re-attach → Task 3 (`HEAL_COOLDOWN_MS`). ✓
- Silent recovery + observability log → Task 3 (`clientLogger.warn`), no user-facing surface. ✓
- Baseline reset on `replayReset` → Task 3 Step 4. ✓
- Extra (found during planning): harden batcher/`to_wire` so one bad event can't freeze the socket → Task 1 (`_redact_buffer`, per-event try/except). Additive, in-scope for "lost completely."

**Placeholder scan:** none — every step carries real code and exact commands.

**Type consistency:** `GapState`, `createGapState`, `resetSeq`, `handleLiveBatch` signatures identical across Tasks 2 and 3; `_forward_live` wire keys (`seq`, `sessionId`) match what `handleLiveBatch` reads.

**Out of scope (this PR):** freshness `updatedAt` single-sourcing (PR 2), poll/WS version-guarded merge (PR 3), heartbeat for a fully-idle stalled stream (design lists it under the not-chosen option). No schema changes, so no RPC/migration parity work required here.
