# Deploy-time Input Hold & Always-Visible Banner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Never silently drop user input during a deploy/disconnect, and always show a banner while input is being held.

**Architecture:** The browser already buffers prompts while a deploy `updatePending` signal is active. This plan generalizes that: the client buffers whenever the socket is not open *or* the server is draining, flushes on reconnect, bounds the buffer, and the server sends a `promptRejected` ack so a prompt refused mid-drain is re-queued instead of lost. One banner renders whenever input is held (drain **or** disconnect).

**Tech Stack:** React + TypeScript (Vitest) frontend in `web/`; Python FastAPI + SQLAlchemy backend in `server/`; native `claude` sessions in PTYs bridged over a WebSocket; a daemon/edge split communicating over a Unix-socket RPC in remote mode.

Spec: `docs/superpowers/specs/2026-06-23-deploy-input-hold-and-banner-design.md`

## Global Constraints

- **Web type gate is `npx tsc -b --force`** — `tsc --noEmit` checks nothing in this repo (root `tsconfig` is `files:[]`+refs).
- **ESLint runs with `--max-warnings 0`** and the `react-hooks` rules; run `npm run lint` in `web/` before claiming done.
- **Run the FULL vitest suite** (`npx vitest run` in `web/`) — CI runs the full suite, not the changed subset.
- **Backend gates: ruff + mypy + pytest**, in that order. Run all three.
- **Worktree env:** this is a fresh worktree with no built env. Run backend tests with the main checkout's venv and `PYTHONPATH=<worktree>/src`, e.g.
  `PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/<main>/.venv/bin/python -m pytest …` — resolve the actual prebuilt `.venv` first (`find /mnt/ssk-ssd/tony/GitHub/.ccswitch -maxdepth 4 -name pytest -path '*/.venv/*' | head`). Symlink `web/node_modules` from the deploy worktree and use the local `.bin` (not `npx`).
- **No silent caps:** the bounded prompt buffer (cap **50**) must surface a visible warning when it drops a message.
- **Buffer cap = 50 prompts**, FIFO, drop-oldest on overflow.

---

### Task 1: Client — never silently drop input (buffer on closed socket, bounded, flush on reconnect)

Generalizes `SessionSocket.prompt()` so a keystroke is buffered whenever it cannot be trusted to land (socket not `OPEN` **or** draining), bounds the buffer at 50 with a visible warning, and flushes the buffer on every reconnect after the re-`attach`.

**Files:**
- Modify: `web/src/ws/client.ts` (fields ~73-75; `prompt()` 158-165; `onopen` 101-109; `onclose` 124-129; `_flushPending` 211-221; add `_enqueuePrompt`)
- Test: `web/src/ws/client.test.ts` (add cases to the `SessionSocket update-pending queue` describe block, ~line 358)

**Interfaces:**
- Consumes: existing `SessionSocket` (`_pendingPrompts`, `_updatePending`, `_emitUpdatePending`, `_flushPending`, `_send`, `updatePendingState`).
- Produces: `prompt(sessionId, text, attachmentIds?)` now buffers on a closed socket; `private _enqueuePrompt(p: { sessionId: string; text: string; attachmentIds?: string[] }): void`; `static MAX_PENDING = 50`. `_droppedWhilePending` is REMOVED. Later tasks (Task 4) reuse `_enqueuePrompt`.

- [ ] **Step 1: Write failing tests**

Add to `web/src/ws/client.test.ts` inside the existing `describe('SessionSocket update-pending queue', …)` block (after the last `it`, before the closing `})` at line 429):

```ts
  it('buffers a prompt typed while disconnected (no drain) and flushes it on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()
    sock.attach('s1')

    ws.simulateClose()                       // socket CLOSED, NOT draining
    sock.prompt('s1', 'typed offline', [])   // must buffer, not drop
    expect(sock.updatePendingState.count).toBe(1)

    sock.connect()
    ws = lastSock()
    ws.simulateOpen()
    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('typed offline'))).toBe(true)
  })

  it('drops the oldest held prompt and surfaces an error when the buffer is full', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    const ws = lastSock()
    ws.simulateOpen()
    ws.simulateMessage({ type: 'updatePending' })   // enter holding so prompts buffer

    const errors: { code?: string }[] = []
    sock.on('error', (e) => errors.push(e as { code?: string }))
    for (let i = 0; i < 51; i++) sock.prompt('s1', `m${i}`, [])

    expect(sock.updatePendingState.count).toBe(50)        // capped, not 51
    expect(errors.some((e) => e.code === 'PENDING_BUFFER_FULL')).toBe(true)
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/ws/client.test.ts -t "buffer"`
Expected: FAIL — `buffers a prompt typed while disconnected` fails (prompt dropped, count 0); `drops the oldest` fails (count 51, no error).

- [ ] **Step 3: Implement the client changes**

In `web/src/ws/client.ts`:

(a) Replace the three pending-buffer fields (lines 73-75) — remove `_droppedWhilePending`, add the cap:

```ts
  private _updatePending = false
  private _pendingPrompts: { sessionId: string; text: string; attachmentIds?: string[] }[] = []
  private static readonly MAX_PENDING = 50
```

(b) Replace `prompt()` (lines 158-165):

```ts
  prompt(sessionId: string, text: string, attachmentIds?: string[]) {
    // Never silently drop input. Buffer whenever it can't be trusted to land — the
    // server is draining (updatePending) OR the socket isn't open — and flush on the
    // next reconnect (after re-attach). Otherwise send straight through.
    if (this._updatePending || this.ws?.readyState !== WebSocket.OPEN) {
      this._enqueuePrompt({ sessionId, text, attachmentIds })
      return
    }
    this._send({ type: 'prompt', sessionId, text, ...(attachmentIds && attachmentIds.length ? { attachmentIds } : {}) })
  }
```

(c) In `connect()`, replace the `onopen` flush guard (line 108) and drop the `onclose` `_droppedWhilePending` line (line 126):

```ts
    ws.onopen = () => {
      this._reconnectAttempt = 0
      this._setConnectionState('connected')
      // Re-attach active session so server replays session history
      if (this._activeSessionId) {
        this._send({ type: 'attach', sessionId: this._activeSessionId })
      }
      if (this._pendingPrompts.length) this._flushPending()
    }
```

```ts
    ws.onclose = () => {
      if (this._destroyed) return
      this._setConnectionState('disconnected')
      this._scheduleReconnect()
    }
```

(d) Add `_enqueuePrompt` and update `_flushPending` (remove the `_droppedWhilePending` reset). Place `_enqueuePrompt` just above `_flushPending` (line 211):

```ts
  private _enqueuePrompt(p: { sessionId: string; text: string; attachmentIds?: string[] }) {
    if (this._pendingPrompts.length >= SessionSocket.MAX_PENDING) {
      const dropped = this._pendingPrompts.shift()
      // No silent caps — tell the user a held message was lost rather than dropping it quietly.
      this._emit('error', {
        type: 'error',
        code: 'PENDING_BUFFER_FULL',
        message: `Too many unsent messages (${SessionSocket.MAX_PENDING}) — the oldest was dropped.`,
      })
      if (dropped) console.warn('[ws] pending-prompt buffer full — dropped oldest', dropped.sessionId)
    }
    this._pendingPrompts.push(p)
    this._emitUpdatePending()
  }

  private _flushPending() {
    const pending = this._pendingPrompts
    this._pendingPrompts = []
    this._updatePending = false
    for (const p of pending) {
      this._send({ type: 'prompt', sessionId: p.sessionId, text: p.text,
        ...(p.attachmentIds && p.attachmentIds.length ? { attachmentIds: p.attachmentIds } : {}) })
    }
    this._emitUpdatePending()
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/ws/client.test.ts`
Expected: PASS — new cases green AND the existing `update-pending queue` cases (buffers while pending, flushes on updateCancelled, emits change) still pass.

- [ ] **Step 5: Type-check and lint**

Run (in `web/`): `npx tsc -b --force && npm run lint`
Expected: no errors, 0 warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.test.ts
git commit -m "fix(web): buffer prompts on any closed socket, bound the buffer, flush on reconnect

Input typed while disconnected (deploy edge restart, network blip) is no longer
silently dropped — it is held (cap 50, drop-oldest with a visible warning) and
flushed after the next reconnect+re-attach.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Server — `send_prompt` returns a delivery status across all layers

`SessionOrchestrator.send_prompt` currently returns `None`. Make it return a status string so the WS bridge (Task 3) can tell the browser a prompt was refused. Thread the value through the daemon RPC handler and `RemoteOrchestrator` so it survives the daemon→edge boundary in remote mode (the facade already passes returns through).

**Files:**
- Modify: `server/sessions/orchestrator.py` (`send_prompt`, 1133-1156)
- Modify: `server/ipc/orchestrator_rpc.py` (`send_prompt` handler, 50-52)
- Modify: `server/sessions/remote.py` (`RemoteOrchestrator.send_prompt`, 105-106)
- Test: `server/tests/test_orchestrator_drain.py` (add 2 cases after line 87)

**Interfaces:**
- Consumes: existing `self._draining`, `self._drivers`, `self._publish_event`.
- Produces: `async def send_prompt(self, session_id, text, attachment_ids=None) -> str` returning one of `"draining"` | `"no_driver"` | `"sent"`. The daemon RPC `send_prompt` handler returns `{"status": <that string>}`. `RemoteOrchestrator.send_prompt(...) -> str | None` returns `result["status"]`. Task 3 consumes the `"draining"` value in `ws.py`.

- [ ] **Step 1: Write failing tests**

Add to `server/tests/test_orchestrator_drain.py` after line 87 (end of file):

```python
@pytest.mark.asyncio
async def test_send_prompt_returns_draining_status():
    o = _orch()
    await o.begin_drain(selfclear_sec=999)
    # The drain guard returns BEFORE touching _drivers, so an unknown id is fine.
    assert await o.send_prompt("unknown-session", "hello") == "draining"
    o.cancel_drain()


@pytest.mark.asyncio
async def test_send_prompt_returns_no_driver_when_idle_and_unknown():
    o = _orch()
    assert await o.send_prompt("unknown-session", "hello") == "no_driver"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/test_orchestrator_drain.py -k "status or no_driver" -q`
Expected: FAIL — `send_prompt` returns `None`, so both `== "draining"` / `== "no_driver"` assertions fail.

- [ ] **Step 3: Implement the status return (orchestrator)**

In `server/sessions/orchestrator.py`, replace `send_prompt` (1133-1156):

```python
    async def send_prompt(self, session_id, text, attachment_ids=None) -> str:
        if self._draining:
            # No new turns during a deploy drain. Tell the browser so it re-queues
            # (promptRejected) and shows the hold banner — never silently swallow it.
            log.info("send_prompt rejected: drain in progress session=%s", session_id)
            self._publish_event({"type": "updatePending"})
            return "draining"
        d = self._drivers.get(session_id)
        if not d:
            return "no_driver"
        ids = list(attachment_ids or [])
        if len(ids) > _attachments.MAX_PER_MESSAGE:
            log.warning("send_prompt: capping attachments from %d to %d session=%s",
                        len(ids), _attachments.MAX_PER_MESSAGE, session_id)
        resolved: list[dict] = []
        if ids:
            # Bind rows to this session + a fresh turn id, then resolve the
            # content-addressed on-disk paths the driver loads into image blocks.
            turn_id = uuid.uuid4().hex
            async with db_session() as db:
                resolved = await _attachments.resolve_and_bind(
                    db, session_id, turn_id, ids)
        self._last_prompt[session_id] = text
        await d.send_prompt(text, resolved)
        return "sent"
```

- [ ] **Step 4: Thread the status through the RPC + remote layers**

In `server/ipc/orchestrator_rpc.py`, replace the `send_prompt` handler (50-52):

```python
    async def send_prompt(params: dict) -> dict:
        status = await orch.send_prompt(params["session_id"], params["text"], params.get("images"))
        return {"status": status}
```

In `server/sessions/remote.py`, replace `RemoteOrchestrator.send_prompt` (105-106):

```python
    async def send_prompt(self, session_id: str, text: str, images=None) -> str | None:
        r = await self._client.call("send_prompt", session_id=session_id, text=text, images=images)
        return (r or {}).get("status")
```

- [ ] **Step 5: Run tests + lint to verify they pass**

Run:
```
PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/test_orchestrator_drain.py -q
PYTHONPATH=$(pwd)/src <venv>/bin/python -m ruff check server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py server/sessions/remote.py
PYTHONPATH=$(pwd)/src <venv>/bin/python -m mypy server/sessions/orchestrator.py server/sessions/remote.py server/ipc/orchestrator_rpc.py
```
Expected: pytest PASS (all drain tests, including the existing `test_send_prompt_rejected_while_draining`); ruff clean; mypy clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_orchestrator_drain.py
git commit -m "feat(server): send_prompt returns a delivery status (draining/no_driver/sent)

Threads the status through the daemon RPC handler and RemoteOrchestrator so the
WS bridge can tell the browser when a prompt was refused mid-drain. No behaviour
change yet — Task 3 consumes the 'draining' status.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Server — `ws.py` emits `promptRejected` when a prompt is refused during drain

The WS bridge calls `orchestrator.send_prompt` but ignores its result, so a drain-refused prompt vanishes with no client feedback. Emit a `promptRejected` frame back to the browser when the status is `"draining"`.

**Files:**
- Modify: `server/bridge/ws.py` (prompt branch, 445-451; local `_send` helper is at 112)
- Test: `server/tests/test_ws.py` (add one case using the `ws_env` fixture)

**Interfaces:**
- Consumes: `orchestrator.send_prompt(...) -> str` from Task 2; the local `async def _send(msg: dict)` (ws.py:112).
- Produces: an outbound frame `{"type": "promptRejected", "sessionId": str, "text": str, "attachmentIds": list|None, "reason": "draining"}`. Task 4 consumes it on the client.

- [ ] **Step 1: Write a failing test**

Add to `server/tests/test_ws.py` (end of file). It creates a live session, attaches, drains the orchestrator, sends a prompt, and asserts a `promptRejected` frame comes back:

```python
def test_prompt_during_drain_returns_promptRejected(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-drain")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        # Enter the deploy drain, then send a prompt — it must be refused AND acked.
        client.portal.call(orch.begin_drain)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "held please"}))

        events = _drain_events(ws, until_type="promptRejected", timeout=8.0)
        rejected = [e for e in events if e.get("type") == "promptRejected"]
        assert rejected, f"no promptRejected in {[e.get('type') for e in events]}"
        assert rejected[0]["sessionId"] == session_id
        assert rejected[0]["text"] == "held please"
        assert rejected[0]["reason"] == "draining"

    # No cancel_drain needed: the fixture tears down the app/loop on exit, which
    # cancels the in-memory drain + its self-clear timer task.
    client.portal.call(orch.close_session, session_id)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/test_ws.py -k promptRejected -q`
Expected: FAIL — no `promptRejected` frame is ever sent (the bridge ignores `send_prompt`'s return), so `_drain_events` times out and `rejected` is empty.

- [ ] **Step 3: Implement the emission**

In `server/bridge/ws.py`, replace the `prompt` branch (445-451):

```python
                elif msg_type == "prompt":
                    # Resume a read-only (detached) session lazily on first input, then
                    # send. send_prompt no-ops without a live driver, so the resume must
                    # happen here — not on attach, where it would block mere viewing.
                    if await _ensure_live(session_id):
                        text = msg.get("text", "")
                        attachment_ids = msg.get("attachmentIds")
                        status = await orchestrator.send_prompt(session_id, text, attachment_ids)
                        if status == "draining":
                            # Deploy drain refused the turn — ack it so the browser
                            # re-queues instead of losing the prompt.
                            await _send({"type": "promptRejected", "sessionId": session_id,
                                         "text": text, "attachmentIds": attachment_ids,
                                         "reason": "draining"})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/test_ws.py -k promptRejected -q`
Expected: PASS.

- [ ] **Step 5: Full ws suite + lint**

Run:
```
PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/test_ws.py -q
PYTHONPATH=$(pwd)/src <venv>/bin/python -m ruff check server/bridge/ws.py
PYTHONPATH=$(pwd)/src <venv>/bin/python -m mypy server/bridge/ws.py
```
Expected: all PASS/clean (no regression in the other ws tests).

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws.py
git commit -m "feat(server): ack a drain-refused prompt with a promptRejected frame

When a prompt arrives during a deploy drain, send promptRejected back to the
browser (sessionId/text/attachmentIds/reason) so the client re-queues it instead
of the keystroke vanishing.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Client — re-queue a `promptRejected` frame

Handle the new server ack: re-queue the refused prompt (via Task 1's `_enqueuePrompt`) and enter holding so it flushes on the next reconnect/cancel.

**Files:**
- Modify: `web/src/ws/client.ts` (add `WsPromptRejectedMsg` ~line 26; add to `WsInboundMsg` union; handle in `_handleInbound` ~288)
- Test: `web/src/ws/client.test.ts` (add to the `update-pending queue` describe block)

**Interfaces:**
- Consumes: `_enqueuePrompt`, `_updatePending`, `_emitUpdatePending` (Task 1).
- Produces: inbound type `WsPromptRejectedMsg = { type: 'promptRejected'; sessionId: string; text: string; attachmentIds?: string[]; reason?: string }`.

- [ ] **Step 1: Write a failing test**

Add to `web/src/ws/client.test.ts` inside `describe('SessionSocket update-pending queue', …)`:

```ts
  it('re-queues a promptRejected frame and flushes it on reconnect', () => {
    const sock = new SessionSocket('/ws')
    sock.connect()
    let ws = lastSock()
    ws.simulateOpen()

    ws.simulateMessage({ type: 'promptRejected', sessionId: 's1', text: 'refused', attachmentIds: [] })
    expect(sock.updatePendingState).toEqual({ active: true, count: 1 })

    ws.simulateClose()
    sock.connect()
    ws = lastSock()
    ws.simulateOpen()
    expect(ws.sent.some((s) => s.includes('"type":"prompt"') && s.includes('refused'))).toBe(true)
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.test.ts -t "re-queues a promptRejected"`
Expected: FAIL — `promptRejected` is unhandled (falls through to the `default` branch, no buffering), so `updatePendingState` stays `{active:false,count:0}`.

- [ ] **Step 3: Implement the handler**

In `web/src/ws/client.ts`, add the message type after `WsUpdateCancelledMsg` (line 25):

```ts
export interface WsPromptRejectedMsg   { type: 'promptRejected'; sessionId: string; text: string; attachmentIds?: string[]; reason?: string }
```

Add it to the `WsInboundMsg` union (after `WsUpdateCancelledMsg`, line 36):

```ts
  | WsUpdateCancelledMsg
  | WsPromptRejectedMsg
```

Add a case in `_handleInbound` (after the `updateCancelled` case, line 294):

```ts
      case 'promptRejected':
        // Server refused this prompt (deploy drain). Re-queue it so it flushes on
        // the next reconnect/cancel instead of being lost, and enter holding.
        this._updatePending = true
        this._enqueuePrompt({ sessionId: msg.sessionId, text: msg.text, attachmentIds: msg.attachmentIds })
        break
```

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/client.test.ts`
Expected: PASS — the re-queue case is green and no existing case regresses.

- [ ] **Step 5: Type-check and lint**

Run (in `web/`): `npx tsc -b --force && npm run lint`
Expected: no errors, 0 warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.test.ts
git commit -m "fix(web): re-queue a server promptRejected ack instead of losing the prompt

A prompt refused mid-drain (server promptRejected frame) is pushed back into the
held buffer and re-sent on the next reconnect, closing the in-flight-during-drain
drop window.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: UI — always-visible hold banner (drain OR disconnected)

Today the banner renders only on `updatePending.active` (`Dashboard.tsx:1676`), so an edge-only deploy or a network drop shows nothing. Introduce `useInputHold()` — holding when draining **or** not connected — wire the banner to it, give the banner a `mode` so the copy adapts, and delete the now-superseded `useUpdatePending` hook.

**Files:**
- Create: `web/src/hooks/useInputHold.ts`
- Delete: `web/src/hooks/useUpdatePending.ts` (superseded — purge in the same change)
- Modify: `web/src/components/UpdatePendingBanner.tsx` (add `mode` prop)
- Modify: `web/src/components/Dashboard.tsx` (import 12; usage 1321; render 1676-1677)
- Test: `web/src/components/__tests__/UpdatePendingBanner.test.tsx` (add a reconnecting case)
- Test: `web/src/hooks/__tests__/useInputHold.test.tsx` (new — degraded-path guard)

**Interfaces:**
- Consumes: `getSessionSocket`, `ConnectionState`, `socket.updatePendingState`, `socket.connectionState`, the `updatePendingChange` and `connectionStateChange` events (all existing in `client.ts`).
- Produces: `useInputHold(): { holding: boolean; count: number; mode: 'updating' | 'reconnecting' }`; `UpdatePendingBanner` gains `mode?: 'updating' | 'reconnecting'` (default `'updating'`).

- [ ] **Step 1: Write failing tests**

Create `web/src/hooks/__tests__/useInputHold.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useInputHold } from '../useInputHold'

describe('useInputHold', () => {
  it('degrades to not-holding when the socket is unavailable (jsdom)', () => {
    // jsdom rejects the relative '/ws' WebSocket URL, so the hook must degrade safely.
    const { result } = renderHook(() => useInputHold())
    expect(result.current.holding).toBe(false)
    expect(result.current.count).toBe(0)
  })
})
```

Add to `web/src/components/__tests__/UpdatePendingBanner.test.tsx` (inside the existing `describe`):

```tsx
  it('shows a reconnecting message and held count when mode is reconnecting', () => {
    render(<UpdatePendingBanner queuedCount={1} mode="reconnecting" />)
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument()
    expect(screen.getByText(/1 message/i)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/hooks/__tests__/useInputHold.test.tsx src/components/__tests__/UpdatePendingBanner.test.tsx`
Expected: FAIL — `useInputHold` does not exist (import error); the banner has no `mode` prop so the reconnecting case can't render the new copy.

- [ ] **Step 3: Create `useInputHold` and generalize the banner**

Create `web/src/hooks/useInputHold.ts`:

```ts
import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'

export interface InputHoldState {
  holding: boolean
  count: number
  mode: 'updating' | 'reconnecting'
}

function tryGetSocket() {
  try {
    return getSessionSocket()
  } catch (e) {
    // jsdom rejects relative WebSocket URLs ('/ws') with SYNTAX_ERR; degrade under test.
    if (e instanceof DOMException && e.code === DOMException.SYNTAX_ERR) return null
    throw e
  }
}

export function useInputHold(): InputHoldState {
  const socket = tryGetSocket()
  const [pending, setPending] = useState(socket ? socket.updatePendingState : { active: false, count: 0 })
  const [conn, setConn] = useState<ConnectionState>(socket ? socket.connectionState : 'connected')
  useEffect(() => {
    if (!socket) return
    const ph = (s: { active: boolean; count: number }) => setPending(s)
    const ch = (s: ConnectionState) => setConn(s)
    socket.on('updatePendingChange', ph)
    socket.on('connectionStateChange', ch)
    return () => {
      socket.off('updatePendingChange', ph)
      socket.off('connectionStateChange', ch)
    }
  }, [socket])
  const connected = conn === 'connected'
  return {
    holding: pending.active || !connected,
    count: pending.count,
    // 'updating' is the richer deploy message; otherwise we're just reconnecting.
    mode: pending.active ? 'updating' : 'reconnecting',
  }
}
```

Replace `web/src/components/UpdatePendingBanner.tsx` body:

```tsx
import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface UpdatePendingBannerProps {
  queuedCount: number
  mode?: 'updating' | 'reconnecting'
}

export function UpdatePendingBanner({ queuedCount, mode = 'updating' }: UpdatePendingBannerProps) {
  const title = mode === 'updating' ? 'Update in progress' : 'Reconnecting…'
  const detail = mode === 'updating'
    ? 'Finishing the current turn, then applying a brief restart.'
    : 'Connection lost — your messages are held and will send once it reconnects.'
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-3 rounded-lg border border-sky-300 dark:border-sky-700',
        'bg-sky-50 dark:bg-sky-900/20 px-4 py-3',
      )}
    >
      <RefreshCw size={18} className="text-sky-700 dark:text-sky-400 flex-shrink-0 mt-0.5 animate-spin" aria-hidden="true" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-sky-900 dark:text-sky-200">{title}</p>
        <p className="text-xs text-sky-700 dark:text-sky-400 mt-0.5">
          {detail}
          {queuedCount > 0 ? (
            <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} held.</>
          ) : null}
        </p>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Wire Dashboard to `useInputHold` and delete the old hook**

In `web/src/components/Dashboard.tsx`:

Line 12 — replace the import:
```tsx
import { useInputHold } from '@/hooks/useInputHold'
```

Line 1321 — replace the hook call:
```tsx
  const inputHold = useInputHold()
```

Lines 1676-1677 — render on holding with the adaptive mode:
```tsx
      {inputHold.holding && (
        <UpdatePendingBanner queuedCount={inputHold.count} mode={inputHold.mode} />
```

Delete the superseded hook:
```bash
git rm web/src/hooks/useUpdatePending.ts
```

- [ ] **Step 5: Run tests, type-check, lint**

Run (in `web/`):
```
npx vitest run
npx tsc -b --force
npm run lint
```
Expected: full suite PASS (including the existing `UpdatePendingBanner` cases — default `mode='updating'` preserves the "Update in progress" copy and the held-count line); `tsc -b` clean (proves nothing still imports the deleted `useUpdatePending`); 0 lint warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/hooks/useInputHold.ts web/src/components/UpdatePendingBanner.tsx web/src/components/Dashboard.tsx web/src/components/__tests__/UpdatePendingBanner.test.tsx web/src/hooks/__tests__/useInputHold.test.tsx
git commit -m "feat(web): show the hold banner whenever input is held (drain OR disconnect)

useInputHold drives one banner from connection-state, so an edge-only deploy or a
network blip now shows 'Reconnecting… messages held' instead of nothing. Replaces
useUpdatePending (deleted).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] **Web:** in `web/` run `npx vitest run`, `npx tsc -b --force`, `npm run lint` — all green, 0 warnings.
- [ ] **Server:** run `ruff check server/`, `mypy server/`, and `PYTHONPATH=$(pwd)/src <venv>/bin/python -m pytest server/tests/ -q` — all green. (Two pre-existing env-only failures — `remote_mode_wiring`, `x11` — are environmental, not regressions.)
- [ ] **Manual trace (optional):** on a daemon deploy, typing during the drain window now shows the banner and re-sends after restart; on an edge-only deploy, the ~1s disconnect shows "Reconnecting… messages held" and flushes on reconnect.

## Known limitation (honest scope note)

The orchestrator status contract is unit-tested (Task 2) and the end-to-end refuse→ack→re-queue path is tested in-process (Tasks 3-4). The **daemon→edge RPC propagation** of the new status (`orchestrator_rpc.py` + `remote.py`) is covered by mypy types but not by a remote-mode integration test in this plan — it mirrors the in-process contract. If a remote-mode regression test harness is added later, assert `send_prompt` RPC returns `{"status": …}`.

## Out of scope

Symptom #3 (session renames continue during a deploy because the titling sweep has no drain/liveness guard) is deferred per the spec. Tracked in the spec's final section.
