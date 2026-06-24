# Persist Queued Messages + Image Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make submitted queued prompts (with their image thumbnails) repopulate the UI when navigating back to a session, and make the unsent composer draft (text + uploaded attachments) survive navigation via localStorage.

**Architecture:** The server-side FIFO in `driver.py` already outlives a WebSocket attach. Part A widens the `QueueUpdate` wire frame to carry per-item images and re-emits the full queue on reattach, so the chip strip repopulates with thumbnails. Part B persists the composer's unsent text + attachment ids to `localStorage`, keyed by session id, and rehydrates on mount.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React + TypeScript (vitest) frontend.

## Global Constraints

- **Backend tests** run against the prebuilt env with the worktree on the path:
  `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest …`
  (a fresh worktree cannot `uv sync`). Two pre-existing env-only failures
  (`remote_mode_wiring`, `x11`) are not regressions.
- **Frontend tests** run in `web/` via the local binary: `./node_modules/.bin/vitest run …`
  (symlink `web/node_modules` from the deploy worktree first if absent). The real
  type gate is `npx tsc -b --force`, NOT `tsc --noEmit` (which checks nothing here).
- **No DB schema change** in this plan — draft state lives in the browser only.
- Follow existing patterns; keep `driver.queued()` returning `list[str]` (existing
  tests and the status `count` depend on it) — add the richer shape alongside it.

---

### Task 1: Structured `QueueUpdate` wire frame + driver `queue_wire()`

Widen the queue event so each item carries its text and image refs, and have the
driver build that shape. `queued()` (texts only) stays for status/count.

**Files:**
- Modify: `server/sessions/events.py` (the `QueueUpdate` dataclass, ~line 130)
- Modify: `server/sessions/driver.py` (queue emit sites ~lines 283, 400, 417, 492; `queued()` ~408)
- Test: `server/tests/test_events.py`, `server/tests/test_driver.py`

**Interfaces:**
- Produces: `QueueUpdate(items: list[dict])` where each item is
  `{"text": str, "images": [{"url": str, "mime": str, "name": str}]}`.
- Produces: `SessionDriver.queue_wire() -> list[dict]` returning that item list from
  `self._queue` (each entry is `(text, images)`, `images` dicts have `url`/`mime`/`name`).

- [ ] **Step 1: Write the failing events test**

In `server/tests/test_events.py` add:

```python
from server.sessions.events import QueueUpdate

def test_queue_update_wire_carries_text_and_images():
    qu = QueueUpdate([
        {"text": "hello", "images": []},
        {"text": "look", "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]},
    ])
    assert qu.to_wire() == {
        "type": "queueUpdate",
        "items": [
            {"text": "hello", "images": []},
            {"text": "look", "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]},
        ],
    }
```

- [ ] **Step 2: Write the failing driver test**

In `server/tests/test_driver.py` add (mirrors `test_concurrent_send_prompt_enqueues`):

```python
def test_queue_wire_includes_images_for_queued_prompt():
    """A prompt queued while busy is exposed via queue_wire() with its image refs."""
    import asyncio

    class SlowFakeSDK:
        def __init__(self): self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt): self.queries.append(prompt)
        async def receive_response(self):
            await asyncio.sleep(0.05)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = SlowFakeSDK()
    def factory(cwd, cfg, can_use_tool, mode, resume): return sdk
    images = [{"sha": "a", "mime": "image/png", "name": "s.png",
               "path": "/tmp/x", "url": "/api/attachments/a"}]

    async def run():
        d = SessionDriver("sq", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")            # starts the (slow) turn
        await d.send_prompt("second", images)   # queues behind it
        wire = d.queue_wire()
        await d.aclose()
        return wire

    wire = asyncio.run(run())
    assert wire == [{"text": "second",
                     "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]}]
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_events.py::test_queue_update_wire_carries_text_and_images server/tests/test_driver.py::test_queue_wire_includes_images_for_queued_prompt -v`
Expected: FAIL — `test_queue_wire...` errors with `AttributeError: 'SessionDriver' object has no attribute 'queue_wire'`.

- [ ] **Step 4: Update the `QueueUpdate` dataclass**

In `server/sessions/events.py`, replace the dataclass:

```python
@dataclass
class QueueUpdate:
    items: list[dict]  # each: {"text": str, "images": [{"url","mime","name"}]}
    def to_wire(self): return {"type": "queueUpdate", "items": self.items}
```

- [ ] **Step 5: Add `queue_wire()` and route all emits through it**

In `server/sessions/driver.py`, add a builder next to `queued()`:

```python
    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot: text + image refs per queued prompt."""
        return [
            {"text": t,
             "images": [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                        for im in imgs]}
            for t, imgs in self._queue
        ]
```

Then replace every `ev.QueueUpdate([t for t, _ in self._queue])` (the append site
~line 283, the drain-pop site ~line 400, and `cancel_queued` ~line 417) with
`ev.QueueUpdate(self.queue_wire())`, and replace the stop-clear `ev.QueueUpdate([])`
(~line 492) with `ev.QueueUpdate([])` unchanged (empty list is already valid). Leave
`queued()` returning `[t for t, _ in self._queue]`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_events.py server/tests/test_driver.py -v`
Expected: PASS (all driver queue tests, including the existing `queued()`-based ones, stay green).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/events.py server/sessions/driver.py server/tests/test_events.py server/tests/test_driver.py
git commit -m "feat(queue): QueueUpdate carries per-item images; add driver.queue_wire()"
```

---

### Task 2: Re-emit the queue on WebSocket attach

A client that reattaches to a live session with queued prompts currently gets only
`Status(count=N)` — never the items. Re-emit the full queue so the chip strip repopulates.

**Files:**
- Modify: `server/bridge/ws.py` (`_emit_live_extras`, ~lines 296-306)
- Test: `server/tests/test_ws.py`

**Interfaces:**
- Consumes: `SessionDriver.queue_wire()` from Task 1.

- [ ] **Step 1: Write the failing test**

In `server/tests/test_ws.py` add (a fresh attach must see the already-queued prompt):

```python
def test_reattach_reemits_queue(ws_env):
    """A client attaching to a live session with a queued prompt receives a
    queueUpdate frame carrying the queued item (regression: previously only the
    status count was re-emitted, so queued chips vanished on navigation)."""
    class BusySDK:
        def __init__(self, msgs, on_can_use_tool=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt): pass
        async def receive_response(self):
            yield f.stream_start(uuid="t1")
            await asyncio.sleep(30)   # keep the turn busy; test ends first
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    _app, orch, client = ws_env([], client_cls=BusySDK)
    session = client.portal.call(orch.create_session, "q-reattach")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws1:
        ws1.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws1, until_type="replayComplete")
        ws1.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "first"}))
        # Wait until the turn is actually running, then queue a second prompt.
        _drain_events(ws1, until_type="status", timeout=8.0)
        ws1.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "second"}))
        _drain_events(ws1, until_type="queueUpdate", timeout=8.0)

        # Fresh attach: must be told about the queued "second" prompt.
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws2:
            ws2.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            events = _drain_events(ws2, until_type="queueUpdate", timeout=8.0)

    qu = [e for e in events if e.get("type") == "queueUpdate"]
    assert qu, f"no queueUpdate on reattach; saw {[e.get('type') for e in events]}"
    assert qu[-1]["items"] == [{"text": "second", "images": []}]

    client.portal.call(orch.close_session, session_id)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py::test_reattach_reemits_queue -v`
Expected: FAIL — `assert qu` fails (no queueUpdate frame on the second attach).

- [ ] **Step 3: Emit the queue in `_emit_live_extras`**

In `server/bridge/ws.py`, inside `_emit_live_extras`, right after the existing
`Status` emit `try/except` block (~line 306), add:

```python
            # Re-emit the live queue so a reattach repopulates the queued-prompt
            # chips (with their images). The driver holds the queue across attaches;
            # without this, a navigate-away/back showed an empty strip. Best-effort.
            try:
                if driver.queued():
                    await _send(redact_event(ev.QueueUpdate(driver.queue_wire()).to_wire()))
            except Exception:
                log.warning("ws.attach: queue re-emit failed for %s", session_id, exc_info=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py::test_reattach_reemits_queue -v`
Expected: PASS.

- [ ] **Step 5: Run the broader ws suite for regressions**

Run: `env -u CCSWITCH_ENABLE_XVFB PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws.py -q`
Expected: PASS (unset `CCSWITCH_ENABLE_XVFB` so the live shell env matches CI).

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws.py
git commit -m "fix(queue): re-emit the live queue on WS attach so queued chips survive navigation"
```

---

### Task 3: Frontend `QueuedItem` type — events + reducer

Mirror the new wire shape on the client so `state.queued` holds structured items.

**Files:**
- Modify: `web/src/session/events.ts` (`EvQueueUpdate`, ~line 22)
- Modify: `web/src/session/reducer.ts` (`TranscriptState.queued` ~line 63, initial ~78, `queueUpdate` case ~544)
- Test: `web/src/session/__tests__/reducer.queue.test.ts` (new)

**Interfaces:**
- Produces: `export interface QueuedItem { text: string; images: { url: string; mime: string; name: string }[] }`
- Produces: `EvQueueUpdate { type: 'queueUpdate'; items: QueuedItem[] }`
- Produces: `TranscriptState.queued: QueuedItem[]`

- [ ] **Step 1: Write the failing reducer test**

Create `web/src/session/__tests__/reducer.queue.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '@/session/reducer'

describe('queueUpdate reducer', () => {
  it('stores structured queued items with images', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'queueUpdate',
      items: [
        { text: 'first', images: [] },
        { text: 'see this', images: [{ url: '/api/attachments/a', mime: 'image/png', name: 's.png' }] },
      ],
    })
    expect(s.queued).toHaveLength(2)
    expect(s.queued[1].text).toBe('see this')
    expect(s.queued[1].images[0].url).toBe('/api/attachments/a')
  })

  it('starts empty', () => {
    expect(initialTranscript.queued).toEqual([])
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/session/__tests__/reducer.queue.test.ts`
Expected: FAIL — type error / `s.queued[1].text` undefined because `items` is still `string[]`.

- [ ] **Step 3: Add the `QueuedItem` type to events**

In `web/src/session/events.ts`, replace the `EvQueueUpdate` line and add the type:

```ts
export interface QueuedItem { text: string; images: { url: string; mime: string; name: string }[] }
export interface EvQueueUpdate   { type: 'queueUpdate';   items: QueuedItem[] }
```

- [ ] **Step 4: Update the reducer state type**

In `web/src/session/reducer.ts`:
- Add the import: `import type { QueuedItem } from '@/session/events'` (or extend the existing events import).
- Change the field (~line 63): `queued: QueuedItem[]              // prompts waiting in the server-side queue`
- The initial value (~line 78) stays `queued: [],`.
- The `queueUpdate` case (~line 544) stays `return { ...state, queued: ev.items }` (now typed as `QueuedItem[]`).

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/session/__tests__/reducer.queue.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/__tests__/reducer.queue.test.ts
git commit -m "feat(web): QueuedItem type carries images through events + reducer"
```

---

### Task 4: Render image thumbnails in the queued chip strip

The chip strip now receives `QueuedItem[]`; render each item's text plus its image
thumbnails. Cancel-by-index is unchanged.

**Files:**
- Modify: `web/src/components/session/Composer.tsx` (`queued` prop ~line 17, chip strip ~lines 237-263)
- Test: `web/src/components/session/__tests__/Composer.queue.test.tsx` (new)

**Interfaces:**
- Consumes: `QueuedItem` from `@/session/events`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/Composer.queue.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { Composer } from '../Composer'

describe('Composer queued strip', () => {
  it('renders queued item text and image thumbnails', () => {
    render(<Composer
      status={{ state: 'working', count: 1 }}
      onPrompt={() => {}} onInterrupt={() => {}}
      queued={[{ text: 'analyze this', images: [{ url: '/api/attachments/a', mime: 'image/png', name: 's.png' }] }]}
    />)
    expect(screen.getByText('analyze this')).toBeTruthy()
    const img = screen.getByAltText('s.png') as HTMLImageElement
    expect(img.src).toContain('/api/attachments/a')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/Composer.queue.test.tsx`
Expected: FAIL — `queued` is typed `string[]`; `item.text` / thumbnail not rendered.

- [ ] **Step 3: Update the prop type**

In `web/src/components/session/Composer.tsx`:
- Add `QueuedItem` to the events import: `import type { Command, QueuedItem } from '@/session/events'`.
- Change the prop (~line 17): `queued?: QueuedItem[]`.

- [ ] **Step 4: Render text + thumbnails**

Replace the queued-strip body (the `queued.map(...)` block, ~lines 239-261) with:

```tsx
          {queued.map((item, index) => (
            <div
              key={`${index}-${item.text.slice(0, 20)}`}
              className={cn(
                'flex items-center justify-between gap-2 px-2 py-1 rounded',
                'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 text-xs',
              )}
            >
              <div className="flex items-center gap-1.5 flex-1 min-w-0">
                {item.images.map((img, i) => (
                  <img
                    key={i}
                    src={img.url}
                    alt={img.name || 'attachment'}
                    className="h-8 w-8 rounded object-cover flex-shrink-0"
                  />
                ))}
                <span className="truncate flex-1">
                  {item.text.length > 80 ? item.text.slice(0, 80) + '…' : item.text}
                </span>
              </div>
              <button
                type="button"
                aria-label="Cancel queued prompt"
                onClick={() => onCancelQueued?.(index)}
                className={cn(
                  'flex-shrink-0 flex items-center justify-center min-h-[44px] min-w-[44px]',
                  'text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                )}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          ))}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/Composer.queue.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/__tests__/Composer.queue.test.tsx
git commit -m "feat(web): show image thumbnails in the queued-prompt chips"
```

---

### Task 5: `useSessionDraft` hook — localStorage-backed draft store

Persist the unsent composer draft (text + attachment refs) per session.

**Files:**
- Create: `web/src/components/session/useSessionDraft.ts`
- Test: `web/src/components/session/__tests__/useSessionDraft.test.ts` (new)

**Interfaces:**
- Produces:
  ```ts
  export interface DraftAttachment { id: string; mime: string; name: string }
  export interface SessionDraft { text: string; attachments: DraftAttachment[] }
  export function useSessionDraft(sessionId?: string): {
    initial: SessionDraft            // read once on mount
    save: (draft: SessionDraft) => void
    clear: () => void
  }
  ```

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/useSessionDraft.test.ts`:

```ts
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { useSessionDraft } from '../useSessionDraft'

const KEY = 'ccswitch:draft:s1'

beforeEach(() => localStorage.clear())

describe('useSessionDraft', () => {
  it('reads an existing draft on mount', () => {
    localStorage.setItem(KEY, JSON.stringify({ text: 'hi', attachments: [{ id: 'a', mime: 'image/png', name: 's.png' }] }))
    const { result } = renderHook(() => useSessionDraft('s1'))
    expect(result.current.initial.text).toBe('hi')
    expect(result.current.initial.attachments[0].id).toBe('a')
  })

  it('save() round-trips and clear() removes the key', () => {
    const { result } = renderHook(() => useSessionDraft('s1'))
    act(() => result.current.save({ text: 'draft', attachments: [] }))
    expect(JSON.parse(localStorage.getItem(KEY)!).text).toBe('draft')
    act(() => result.current.clear())
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('save() of an empty draft removes the key', () => {
    localStorage.setItem(KEY, JSON.stringify({ text: 'old', attachments: [] }))
    const { result } = renderHook(() => useSessionDraft('s1'))
    act(() => result.current.save({ text: '', attachments: [] }))
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('no sessionId is a no-op (empty initial, no writes)', () => {
    const { result } = renderHook(() => useSessionDraft(undefined))
    expect(result.current.initial).toEqual({ text: '', attachments: [] })
    act(() => result.current.save({ text: 'x', attachments: [] }))
    expect(localStorage.length).toBe(0)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/useSessionDraft.test.ts`
Expected: FAIL — module `../useSessionDraft` not found.

- [ ] **Step 3: Implement the hook**

Create `web/src/components/session/useSessionDraft.ts`:

```ts
import { useCallback, useRef } from 'react'

export interface DraftAttachment { id: string; mime: string; name: string }
export interface SessionDraft { text: string; attachments: DraftAttachment[] }

const EMPTY: SessionDraft = { text: '', attachments: [] }
const keyFor = (sessionId: string) => `ccswitch:draft:${sessionId}`

function read(sessionId?: string): SessionDraft {
  if (!sessionId) return EMPTY
  try {
    const raw = localStorage.getItem(keyFor(sessionId))
    if (!raw) return EMPTY
    const parsed = JSON.parse(raw)
    return {
      text: typeof parsed.text === 'string' ? parsed.text : '',
      attachments: Array.isArray(parsed.attachments) ? parsed.attachments : [],
    }
  } catch {
    return EMPTY
  }
}

/**
 * Per-session unsent-draft store backed by localStorage. `initial` is read once
 * on mount (stable for the component's lifetime); call `save` on change and
 * `clear` on submit. A no-op when `sessionId` is undefined.
 */
export function useSessionDraft(sessionId?: string) {
  const initialRef = useRef<SessionDraft>(read(sessionId))

  const clear = useCallback(() => {
    if (!sessionId) return
    try { localStorage.removeItem(keyFor(sessionId)) } catch { /* ignore */ }
  }, [sessionId])

  const save = useCallback((draft: SessionDraft) => {
    if (!sessionId) return
    const empty = draft.text.trim() === '' && draft.attachments.length === 0
    try {
      if (empty) localStorage.removeItem(keyFor(sessionId))
      else localStorage.setItem(keyFor(sessionId), JSON.stringify(draft))
    } catch { /* ignore quota/availability errors */ }
  }, [sessionId])

  return { initial: initialRef.current, save, clear }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/useSessionDraft.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/useSessionDraft.ts web/src/components/session/__tests__/useSessionDraft.test.ts
git commit -m "feat(web): add useSessionDraft localStorage-backed draft store"
```

---

### Task 6: Seed `useImageAttachments` from a persisted draft

Allow the attachments hook to initialize from rehydrated draft attachments, using the
durable server URL as the preview (the blob URL from `createObjectURL` cannot survive a reload).

**Files:**
- Modify: `web/src/components/session/useImageAttachments.ts`
- Test: `web/src/components/session/__tests__/useImageAttachments.test.tsx` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces: `useImageAttachments(initial?: Attachment[])` — when provided, `attachments`
  starts seeded and `refs()` returns the seeded ids immediately.

- [ ] **Step 1: Write the failing test**

Append to `web/src/components/session/__tests__/useImageAttachments.test.tsx`:

```tsx
  it('seeds from initial attachments (rehydrated draft)', () => {
    const initial = [{
      key: 'seed-0', id: 'att-9', mime: 'image/png',
      name: 's.png', previewUrl: '/api/attachments/att-9',
    }]
    const { result } = renderHook(() => useImageAttachments(initial))
    expect(result.current.attachments).toHaveLength(1)
    expect(result.current.attachments[0].previewUrl).toBe('/api/attachments/att-9')
    expect(result.current.refs()).toEqual(['att-9'])
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: FAIL — the new case sees 0 attachments (hook ignores the arg).

- [ ] **Step 3: Accept an initial seed**

In `web/src/components/session/useImageAttachments.ts`, change the signature and state init:

```ts
export function useImageAttachments(initial?: Attachment[]) {
  const [attachments, setAttachments] = useState<Attachment[]>(initial ?? [])
```

(Everything else is unchanged.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: PASS (all existing cases plus the new one).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/useImageAttachments.ts web/src/components/session/__tests__/useImageAttachments.test.tsx
git commit -m "feat(web): seed useImageAttachments from a rehydrated draft"
```

---

### Task 7: Wire the draft into the Composer + SessionComposer

Hydrate the composer from the persisted draft on mount, persist on change, clear on submit.

**Files:**
- Modify: `web/src/components/session/Composer.tsx` (props, `useState`/`useImageAttachments` init, persist effect, `submit`)
- Modify: `web/src/components/session/SessionComposer.tsx` (pass `sessionId`)
- Test: `web/src/components/session/__tests__/Composer.draft.test.tsx` (new)

**Interfaces:**
- Consumes: `useSessionDraft` (Task 5), seeded `useImageAttachments` (Task 6).
- Produces: `Composer` accepts an optional `sessionId?: string` prop.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/Composer.draft.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { Composer } from '../Composer'

beforeEach(() => {
  localStorage.clear()
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
})

describe('Composer draft persistence', () => {
  it('hydrates the textarea from a saved draft', () => {
    localStorage.setItem('ccswitch:draft:s1', JSON.stringify({ text: 'saved draft', attachments: [] }))
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={() => {}} onInterrupt={() => {}} />)
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('saved draft')
  })

  it('persists typed text to localStorage', () => {
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={() => {}} onInterrupt={() => {}} />)
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'work in progress' } })
    expect(JSON.parse(localStorage.getItem('ccswitch:draft:s1')!).text).toBe('work in progress')
  })

  it('clears the draft on send', () => {
    localStorage.setItem('ccswitch:draft:s1', JSON.stringify({ text: 'hello', attachments: [] }))
    const onPrompt = vi.fn()
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={onPrompt} onInterrupt={() => {}} />)
    fireEvent.click(screen.getByLabelText('Send message'))
    expect(onPrompt).toHaveBeenCalledWith('hello', [])
    expect(localStorage.getItem('ccswitch:draft:s1')).toBeNull()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/Composer.draft.test.tsx`
Expected: FAIL — no hydration (textarea empty; draft not cleared).

- [ ] **Step 3: Wire the draft into the Composer**

In `web/src/components/session/Composer.tsx`:

Add the import:
```tsx
import { useSessionDraft, type DraftAttachment } from './useSessionDraft'
```

Add `sessionId` to `Props`:
```tsx
  sessionId?: string
```
and to the destructured params:
```tsx
export function Composer({ status, onPrompt, onInterrupt, queued, onCancelQueued, chatArmed, commands = [], onCommand, autoFocusKey, suggestions, history = [], onRequestSuggestions, sessionId }: Props) {
```

Replace the `text` and `att` initialization (~lines 30, 40):
```tsx
  const draft = useSessionDraft(sessionId)
  const [text, setText] = useState(draft.initial.text)
  // ... (other useState lines unchanged) ...
  const att = useImageAttachments(
    draft.initial.attachments.map((a, i) => ({
      key: `draft-${i}`, id: a.id, mime: a.mime, name: a.name,
      previewUrl: `/api/attachments/${a.id}`,
    })),
  )
```

Add a persist effect (place after `att` is defined):
```tsx
  // Persist the unsent draft so it survives navigation. att.attachments is the
  // dependency; refs()/ids are derived for storage.
  useEffect(() => {
    const attachments: DraftAttachment[] = att.attachments
      .filter((a) => !a.error && a.id)
      .map((a) => ({ id: a.id as string, mime: a.mime, name: a.name }))
    draft.save({ text, attachments })
  }, [text, att.attachments, draft])
```

In `submit` (~line 118), after `setText(''); att.clear()`, add `draft.clear()`:
```tsx
    onPrompt(trimmed, refs)
    setText(''); att.clear(); draft.clear()
```
Also add `draft.clear()` to the command-only early-return branch (~line 114) right
after `setText('')`:
```tsx
      setText(''); draft.clear(); if (textareaRef.current) textareaRef.current.style.height = 'auto'
```
Add `draft` to the `submit` useCallback dependency array.

- [ ] **Step 4: Pass `sessionId` from SessionComposer**

In `web/src/components/session/SessionComposer.tsx`, add to the `<Composer>` props:
```tsx
      sessionId={rt.sessionId}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session/__tests__/Composer.draft.test.tsx`
Expected: PASS.

- [ ] **Step 6: Run the full Composer + session test set for regressions**

Run: `cd web && ./node_modules/.bin/vitest run src/components/session src/session`
Expected: PASS (existing Composer/SessionComposer tests still green — they render without `sessionId`, so the draft hook no-ops).

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/SessionComposer.tsx web/src/components/session/__tests__/Composer.draft.test.tsx
git commit -m "feat(web): persist + restore the unsent composer draft across navigation"
```

---

### Task 8: Full verification (types + suites)

**Files:** none (verification only).

- [ ] **Step 1: Frontend type gate (the real one)**

Run: `cd web && npx tsc -b --force`
Expected: no errors (`tsc --noEmit` is a false-green here — do not rely on it).

- [ ] **Step 2: Frontend lint + full vitest**

Run: `cd web && ./node_modules/.bin/eslint src && ./node_modules/.bin/vitest run`
Expected: PASS.

- [ ] **Step 3: Backend suite**

Run: `env -u CCSWITCH_ENABLE_XVFB PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS except the two known env-only failures (`remote_mode_wiring`, `x11`).

- [ ] **Step 4: Commit any incidental fixes, then open the PR**

Follow the CLAUDE.md completion workflow: push the branch, `gh pr create` (non-draft,
Claude Code trailer), then `gh pr merge <#> --squash --auto`.

---

## Self-Review

**Spec coverage:**
- Submitted-queue persists on navigation → Task 2 (re-emit on attach).
- Queue carries images → Task 1 (wire) + Task 4 (thumbnails).
- Unsent draft persists (localStorage, text + attachments) → Tasks 5, 6, 7.
- Rehydrated attachments use server URL → Task 6 + Task 7.
- Testing (pytest + vitest) → each task is TDD; Task 8 is the full gate.

**Type consistency:** `QueueUpdate(items: list[dict])` (Py) ↔ `QueuedItem`/`EvQueueUpdate.items` (TS) share `{text, images:[{url,mime,name}]}`. `SessionDraft`/`DraftAttachment` and `useSessionDraft`'s `initial`/`save`/`clear` are used identically in Tasks 5 and 7. `useImageAttachments(initial?)` seed shape matches the `Attachment` type and is constructed the same way in Task 7.

**Placeholder scan:** none — every code step shows complete code; `<worktree>`/`<main-checkout>` are environment paths the executor substitutes, not code placeholders.
