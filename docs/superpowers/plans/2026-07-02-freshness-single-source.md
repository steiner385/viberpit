# Freshness Single-Source (PR 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the server the single source of truth for a session's "active … ago" freshness — stop the frontend fabricating `updatedAt` with the client clock, and stop the relative-time label from freezing between polls.

**Architecture:** The WS `status` message carries the server's authoritative `updatedAt` (the same `last_turn_at`-or-`created_at` value the REST endpoint sends), computed via one shared `SessionRecord.updated_at_iso` property that also replaces two existing duplicate copies. The client patch uses that value verbatim (no `new Date()`), rotation stops touching `updatedAt`, and the dashboard card ticks a unified `useNow` clock so the label advances smoothly.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React + TypeScript (vitest) frontend.

## Global Constraints

- Single source of truth (constitution VI): the "active-ago" serialization (`iso_utc(last_turn_at or created_at)`) must exist in exactly one place — a `SessionRecord.updated_at_iso` property — consumed by `to_dict`, the HTTP serializer, and the WS status publish. Do not add a fourth copy.
- Freshness = `last_turn_at` (falls back to `created_at`). NEVER the row-write `updated_at` column. The client NEVER fabricates `updatedAt` from its own clock.
- `iso_utc` (`server/time_iso.py`) tags naive-UTC with `+00:00`; every wire timestamp goes through it.
- Rotation (`rotated`) is not a fresh turn — it must not change `updatedAt`.
- Test env (fresh worktree): server via `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …` from the worktree root; web in `web/` via `npx vitest run` / `npx tsc --noEmit` / `npx eslint` (`web/node_modules` is symlinked). Do NOT run `uv sync` / `npm install`.

---

### Task 1: Server — one `updated_at_iso` property; WS status carries `updatedAt`

**Files:**
- Modify: `server/sessions/types.py` — add property, route `to_dict` through it.
- Modify: `server/api/sessions.py:131` — route `_record_to_dict` through it.
- Modify: `server/sessions/orchestrator.py:769` — add `updatedAt` to the published status event.
- Test: `server/tests/test_updated_at_iso.py` (create)

**Interfaces:**
- Produces: `SessionRecord.updated_at_iso -> str | None` — `iso_utc(self.last_turn_at or self.created_at)`. The WS `status` wire frame gains an `updatedAt: str | None` field.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_updated_at_iso.py
from datetime import datetime
from server.sessions.types import SessionRecord


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id=None,
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_updated_at_iso_prefers_last_turn_at():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=datetime(2021, 6, 1))
    assert r.updated_at_iso == "2021-06-01T00:00:00+00:00"


def test_updated_at_iso_falls_back_to_created_at():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=None)
    assert r.updated_at_iso == "2020-01-01T00:00:00+00:00"


def test_updated_at_iso_none_when_no_timestamps():
    r = _rec(created_at=None, last_turn_at=None)
    assert r.updated_at_iso is None


def test_to_dict_uses_updated_at_iso():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=datetime(2021, 6, 1))
    assert r.to_dict()["updatedAt"] == r.updated_at_iso
```

- [ ] **Step 2: Run to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_updated_at_iso.py -v`
Expected: FAIL — `AttributeError: 'SessionRecord' object has no attribute 'updated_at_iso'`.

- [ ] **Step 3: Add the property and route `to_dict` through it**

In `server/sessions/types.py`, add the property just above `def to_dict` (line 61):

```python
    @property
    def updated_at_iso(self) -> str | None:
        """The 'active … ago' wire timestamp: the last turn's response, falling back to
        creation. NOT the raw `updated_at` row-write column (which any write bumps → the
        '0s ago' pin). Single source for the dashboard freshness label — used by to_dict,
        the HTTP serializer (api/sessions.py), and the live WS status event (orchestrator)."""
        return iso_utc(self.last_turn_at or self.created_at)
```

Then in `to_dict` replace the `active = ...` line (line 67) and the `updatedAt` line (line 76):

```python
        return {
```
(delete `active = self.last_turn_at or self.created_at`), and change line 76 to:

```python
            "updatedAt": self.updated_at_iso,
```

- [ ] **Step 4: Route the HTTP serializer through it**

In `server/api/sessions.py`, replace line 131:

```python
        "updatedAt": iso_utc(record.last_turn_at or record.created_at),
```
with:
```python
        "updatedAt": record.updated_at_iso,
```

- [ ] **Step 5: Add `updatedAt` to the published WS status event**

In `server/sessions/orchestrator.py`, in `_persist_status`, replace line 769:

```python
        self._publish_event({"type": "status", "sessionId": session_id, "status": status})
```
with:
```python
        self._publish_event({"type": "status", "sessionId": session_id, "status": status,
                             "updatedAt": rec.updated_at_iso if rec is not None else None})
```

(`rec` is the `self._records.get(session_id)` fetched at the top of the method; its `last_turn_at` was just advanced above when the status is an ACTIVITY_STATUS, so the published `updatedAt` matches what the next REST poll will return.)

- [ ] **Step 6: Run the new test + regressions**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_updated_at_iso.py server/tests/ -k "record_to_dict or to_dict or persist_status or session_activity or updated_at" -q`
Expected: PASS (new file green; existing serializer/activity tests still green).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/types.py server/api/sessions.py server/sessions/orchestrator.py server/tests/test_updated_at_iso.py
git commit -m "feat(freshness): single-source updated_at_iso; WS status carries authoritative updatedAt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Client — use the server's `updatedAt`, stop fabricating it

**Files:**
- Modify: `web/src/ws/client.ts` — `WsStatusMsg` (`:18`), `_handleInbound` `'status'` (`:312-315`), `_patchSessionStatus` (`:362-369`), `_patchSessionAccount` (`:371-378`).
- Test: `web/src/ws/client.freshness.test.ts` (create)

**Interfaces:**
- Consumes: server WS `status` frame now includes `updatedAt?: string` (Task 1).
- Produces: `_patchSessionStatus(sessionId, status, updatedAt?)` writes the server `updatedAt` when present and leaves the cached value untouched when absent; `_patchSessionAccount` no longer writes `updatedAt`.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/ws/client.freshness.test.ts
import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

function seed(qc: QueryClient, s: Partial<Session>) {
  qc.setQueryData<Session[]>(QK.sessions, [{ id: 's1', status: 'idle', updatedAt: '2020-01-01T00:00:00+00:00', accountName: 'a', ...s } as Session])
}
function makeSocket() {
  const qc = new QueryClient()
  const s = new SessionSocket('/ws') as unknown as { setQueryClient: (q: QueryClient) => void; _handleInbound: (m: unknown) => void }
  s.setQueryClient(qc)
  return { s, qc }
}
const row = (qc: QueryClient) => (qc.getQueryData<Session[]>(QK.sessions) ?? [])[0]

describe('SessionSocket freshness patches', () => {
  it('status patch writes the server updatedAt (not the client clock)', () => {
    const { s, qc } = makeSocket(); seed(qc, {})
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'done', updatedAt: '2021-06-01T00:00:00+00:00' })
    expect(row(qc).status).toBe('done')
    expect(row(qc).updatedAt).toBe('2021-06-01T00:00:00+00:00')
  })

  it('status patch without updatedAt leaves the cached value untouched', () => {
    const { s, qc } = makeSocket(); seed(qc, { updatedAt: '2020-05-05T00:00:00+00:00' })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress' })
    expect(row(qc).status).toBe('in_progress')
    expect(row(qc).updatedAt).toBe('2020-05-05T00:00:00+00:00')
  })

  it('rotation patch does not change updatedAt', () => {
    const { s, qc } = makeSocket(); seed(qc, { updatedAt: '2020-05-05T00:00:00+00:00' })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'a', toAccount: 'b' })
    expect(row(qc).accountName).toBe('b')
    expect(row(qc).updatedAt).toBe('2020-05-05T00:00:00+00:00')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.freshness.test.ts`
Expected: FAIL — the current code fabricates `updatedAt` with `new Date()`, so the first test sees a different value and the rotation test sees a changed `updatedAt`.

- [ ] **Step 3: Add `updatedAt` to `WsStatusMsg` and pass it through**

In `web/src/ws/client.ts` line 18:

```typescript
export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string; updatedAt?: string }
```

In `_handleInbound` `'status'` case (line 314):

```typescript
        this._patchSessionStatus(msg.sessionId, msg.status as Session['status'], msg.updatedAt)
```

- [ ] **Step 4: Use the server value in `_patchSessionStatus`; stop fabricating in both patches**

Replace `_patchSessionStatus` (lines 362-369):

```typescript
  private _patchSessionStatus(sessionId: string, status: Session['status'], updatedAt?: string) {
    const qc = this._queryClient
    if (!qc) return
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      // Use the server's authoritative updatedAt (last_turn_at-based). When the frame
      // omits it, leave the cached value untouched — never fabricate from the client clock.
      return old.map((s) => s.id === sessionId
        ? { ...s, status, ...(updatedAt ? { updatedAt } : {}) }
        : s)
    })
  }
```

Replace `_patchSessionAccount` (lines 371-378) — drop the `updatedAt` fabrication entirely:

```typescript
  private _patchSessionAccount(sessionId: string, accountName: string) {
    const qc = this._queryClient
    if (!qc) return
    // A rotation is not a fresh turn — update the account only, never updatedAt.
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => s.id === sessionId ? { ...s, accountName } : s)
    })
  }
```

- [ ] **Step 5: Run the test + web gates**

Run (in `web/`): `npx vitest run src/ws/client.freshness.test.ts && npx tsc --noEmit && npx eslint src/ws/`
Expected: PASS (3 tests) + clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.freshness.test.ts
git commit -m "fix(freshness): use server updatedAt in WS patches; rotation no longer touches it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Client — unify `useNow`, tick the dashboard "active … ago"

**Files:**
- Modify: `web/src/hooks/useNow.ts` — add an `active` gate.
- Modify: `web/src/components/session/SubAgentList.tsx` — use the shared hook, delete the private one.
- Modify: `web/src/components/SessionCard.tsx` — tick the relative-time label.
- Test: `web/src/hooks/useNow.test.tsx` (extend)

**Interfaces:**
- Consumes: `relTime(iso, now?)` (`web/src/lib/relativeTime.ts`), `agentDuration(agent, now)` (`web/src/components/session/subAgentDuration.ts`).
- Produces: `useNow(intervalMs = 1000, active = true): number` — ticks only while the tab is visible AND `active`; returns `0` until the first post-mount tick (unchanged purity contract).

- [ ] **Step 1: Write the failing test (extend `useNow.test.tsx`)**

Add this test to `web/src/hooks/useNow.test.tsx` (keep existing tests):

```typescript
import { renderHook, act } from '@testing-library/react'
import { vi } from 'vitest'
import { useNow } from './useNow'

it('does not tick while active is false', () => {
  vi.useFakeTimers()
  const { result } = renderHook(() => useNow(1000, false))
  expect(result.current).toBe(0)
  act(() => { vi.advanceTimersByTime(3000) })
  expect(result.current).toBe(0)   // frozen — the gate suppressed all ticks
  vi.useRealTimers()
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/hooks/useNow.test.tsx`
Expected: FAIL — `useNow` ignores the second arg today, so it ticks and `result.current` becomes non-zero.

- [ ] **Step 3: Add the `active` gate to `useNow`**

Replace the body of `web/src/hooks/useNow.ts` (lines 10-25):

```typescript
export function useNow(intervalMs = 1000, active = true): number {
  const visible = usePageVisible()
  const [now, setNow] = useState(0)
  useEffect(() => {
    if (!visible || !active) return
    // Schedule catch-up as a microtask-safe timer callback (avoids synchronous
    // setState-in-effect lint error while still firing before the first interval).
    const catchUpId = setTimeout(() => setNow(Date.now()), 0)
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => {
      clearTimeout(catchUpId)
      clearInterval(id)
    }
  }, [visible, active, intervalMs])
  return now
}
```

Also update the docstring's first line (line 5-6) to mention the `active` gate:

```typescript
 * Current Date.now() ms, advanced every `intervalMs` while the tab is visible
 * and `active`, and frozen while hidden or inactive (composes usePageVisible —
```

- [ ] **Step 4: Point `SubAgentList` at the shared hook**

In `web/src/components/session/SubAgentList.tsx`: delete the private `useNow` (lines 18-27) and its now-unused `useEffect`/`useState` from the line-1 import; add `import { useNow } from '@/hooks/useNow'`. Change line 36 and the duration call (line 40):

```typescript
  // Only tick while something is actually running (and the tab is visible).
  const now = useNow(1000, subAgents.some((a) => a.status === 'running'))
```
```typescript
        const duration = agentDuration(a, now || Date.now())
```

(The `|| Date.now()` covers the one frame before the first tick, when `now` is `0` — preserving the old behavior where the private hook seeded `Date.now()`.)

Line 1 becomes:
```typescript
import { cn } from '@/lib/utils'
```
(remove the `import { useEffect, useState } from 'react'` line if nothing else in the file uses them — verify with a grep; `SubAgentList` otherwise has no React-hook imports).

- [ ] **Step 5: Tick the `SessionCard` "active … ago" label**

In `web/src/components/SessionCard.tsx`: add `import { useNow } from '@/hooks/useNow'` near the other imports; inside the component body (with the other hooks), add:

```typescript
  const now = useNow()
```

Change the render at line 429:

```typescript
                active {relTime(session.updatedAt, now || undefined)}
```

(`now || undefined` lets `relTime` fall back to its `Date.now()` default on the first frame — matching today's behavior — then advances every second.)

- [ ] **Step 6: Run the affected web tests + gates**

Run (in `web/`): `npx vitest run src/hooks/useNow.test.tsx src/components/session/SubAgentList.test.tsx && npx tsc --noEmit && npx eslint src/`
Expected: PASS + clean. If `SubAgentList.test.tsx` asserted timing tied to the old private hook, update it to drive the shared hook's fake timers; do not weaken assertions.

- [ ] **Step 7: Commit**

```bash
git add web/src/hooks/useNow.ts web/src/components/session/SubAgentList.tsx web/src/components/SessionCard.tsx web/src/hooks/useNow.test.tsx
git commit -m "refactor(freshness): unify useNow (add active gate); tick dashboard active-ago

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (PR 2 slice of the design doc):**
- WS `status` carries server `updatedAt` = `iso_utc(last_turn_at or created_at)` → Task 1. ✓
- Single source: one `updated_at_iso` property replaces the two existing duplicates (types.py, api/sessions.py) and feeds the WS publish → Task 1. ✓ (constitution VI — removes duplication rather than adding a third copy)
- Client uses server `updatedAt` verbatim; delete `new Date()` fabrication → Task 2. ✓
- Rotation no longer touches `updatedAt` → Task 2. ✓
- Unify the two `useNow` hooks; `SessionCard` ticks so "active … ago" advances → Task 3. ✓

**Placeholder scan:** none — every step has real code and exact commands.

**Type consistency:** `updated_at_iso -> str | None` matches `iso_utc`'s return; `_patchSessionStatus(sessionId, status, updatedAt?)` matches the `'status'` dispatch; `useNow(intervalMs, active)` matches both call sites.

**Out of scope (this PR):** poll↔WS version-guarded merge (PR 3 — until then a stale in-flight poll can still momentarily overwrite a WS status; acceptable interim); transcript gap-healing (PR 1, merged separately); `_ACTIVE_STATUSES` (verified unrelated). No schema changes, so no migration/RPC-parity work.
