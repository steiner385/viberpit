# Mobile Session Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a mobile user jump directly from one full-screen session to the next/previous session via chevron buttons in the top bar, without returning to the list.

**Architecture:** A new `useSessionNeighbors(sessionId)` hook reads the same ordered session list the dashboard renders (`useSessions` + `useSessionOrdering`) and returns the wraparound prev/next ids. `SessionView` consumes it and renders two chevron buttons that call a new `onNavigateSession` prop. `App.tsx`'s `SessionRoute` wires that prop to `navigate('/session/:id')` and adds `key={sessionId}` so a session change remounts the view for a clean websocket detach/attach.

**Tech Stack:** React + TypeScript, react-router-dom, @tanstack/react-query, vitest + @testing-library/react.

## Global Constraints

- Run all commands from the `web/` directory.
- Verify types with `npx tsc -b` (NOT `tsc --noEmit` — it is a false-green here).
- Lint with `npx eslint` at `--max-warnings 0` (CI gate); no ref-read-during-render.
- Touch targets on interactive top-bar controls: `min-h-[44px]` (mobile tap target).
- Reuse the existing ordering engine — do NOT add a second ordering/sort path.
- New session wire fields are out of scope; this is frontend-only.

---

### Task 1: `useSessionNeighbors` hook

**Files:**
- Create: `web/src/session/useSessionNeighbors.ts`
- Test: `web/src/session/__tests__/useSessionNeighbors.test.tsx`

**Interfaces:**
- Consumes: `useSessions()` from `@/api/hooks` (returns `{ data?: Session[] }`); `useSessionOrdering(sessions)` from `@/session/useSessionOrdering` (returns `{ orderedSessions: Session[], ... }`); `QK` from `@/api/hooks` (test only — `QK.sessions === ['sessions']`).
- Produces:
  ```ts
  export interface SessionNeighbors {
    prevId: string | null
    nextId: string | null
    index: number   // position in the ordered list; -1 if not found
    total: number   // ordered list length
  }
  export function useSessionNeighbors(sessionId: string): SessionNeighbors
  ```
  Wraparound: with `n = total` and `i = index`, `nextId = ordered[(i+1)%n].id`, `prevId = ordered[(i-1+n)%n].id`. Returns null prev/next when `total <= 1` or `index === -1`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/session/__tests__/useSessionNeighbors.test.tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { QK } from '@/api/hooks'
import { useSessionNeighbors } from '../useSessionNeighbors'
import type { Session } from '@/api/types'

function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z', ...over,
  }
}

// Seed the react-query cache directly so useSessions resolves synchronously to
// our fixture list (no network, no act warnings).
function wrapperWith(sessions: Session[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('useSessionNeighbors', () => {
  // Ordering reads persisted prefs from localStorage; clear for determinism.
  // Default sort 'status' with all-idle sessions keeps stable input order.
  beforeEach(() => window.localStorage.clear())

  it('returns prev/next for a middle session', () => {
    const { result } = renderHook(() => useSessionNeighbors('b'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current).toEqual({ prevId: 'a', nextId: 'c', index: 1, total: 3 })
  })

  it('wraps the first session prev to the last', () => {
    const { result } = renderHook(() => useSessionNeighbors('a'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current.prevId).toBe('c')
    expect(result.current.nextId).toBe('b')
  })

  it('wraps the last session next to the first', () => {
    const { result } = renderHook(() => useSessionNeighbors('c'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current.nextId).toBe('a')
    expect(result.current.prevId).toBe('b')
  })

  it('returns null neighbors for a single session', () => {
    const { result } = renderHook(() => useSessionNeighbors('a'), {
      wrapper: wrapperWith([mk('a')]),
    })
    expect(result.current).toEqual({ prevId: null, nextId: null, index: 0, total: 1 })
  })

  it('returns null neighbors when the session is not in the list', () => {
    const { result } = renderHook(() => useSessionNeighbors('z'), {
      wrapper: wrapperWith([mk('a'), mk('b')]),
    })
    expect(result.current).toEqual({ prevId: null, nextId: null, index: -1, total: 2 })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/session/__tests__/useSessionNeighbors.test.tsx`
Expected: FAIL — `Failed to resolve import '../useSessionNeighbors'` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/session/useSessionNeighbors.ts
import { useMemo } from 'react'
import { useSessions } from '@/api/hooks'
import { useSessionOrdering } from './useSessionOrdering'

export interface SessionNeighbors {
  /** Previous session id in the current list order; null when none. */
  prevId: string | null
  /** Next session id in the current list order; null when none. */
  nextId: string | null
  /** Position of the current session in the ordered list; -1 if absent. */
  index: number
  /** Length of the ordered list. */
  total: number
}

/**
 * Resolve the previous/next session ids relative to `sessionId`, walking the
 * SAME ordered list the dashboard renders (group/sort prefs included).
 * Navigation wraps around the ends. Returns null neighbors when there is only
 * one session, or when the current session is not in the ordered list
 * (e.g. archived, or a freshly-created pending session) — the UI hides the
 * chevrons in that case.
 */
export function useSessionNeighbors(sessionId: string): SessionNeighbors {
  const { data } = useSessions()
  const sessions = useMemo(() => data ?? [], [data])
  const { orderedSessions } = useSessionOrdering(sessions)

  return useMemo(() => {
    const total = orderedSessions.length
    const index = orderedSessions.findIndex((s) => s.id === sessionId)
    if (index === -1 || total <= 1) {
      return { prevId: null, nextId: null, index, total }
    }
    const nextId = orderedSessions[(index + 1) % total].id
    const prevId = orderedSessions[(index - 1 + total) % total].id
    return { prevId, nextId, index, total }
  }, [orderedSessions, sessionId])
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/session/__tests__/useSessionNeighbors.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionNeighbors.ts web/src/session/__tests__/useSessionNeighbors.test.tsx
git commit -m "feat(web): add useSessionNeighbors hook for prev/next session ids

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Chevron buttons in `SessionView`

**Files:**
- Modify: `web/src/components/session/SessionView.tsx` (Props interface ~37-41; top bar ~118-156)
- Test: `web/src/components/session/__tests__/SessionView.neighbors.test.tsx` (create)

**Interfaces:**
- Consumes: `useSessionNeighbors` from `@/session/useSessionNeighbors` (Task 1).
- Produces: extends `SessionView` Props with `onNavigateSession?: (id: string) => void`. When neighbors exist, renders a `‹` button (`aria-label="Previous session"`) before the title and a `›` button (`aria-label="Next session"`) after the title; each calls `onNavigateSession(prevId|nextId)`. Both hidden when `prevId === null && nextId === null`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/SessionView.neighbors.test.tsx
/**
 * SessionView prev/next chevron navigation. Mocks useSessionSocket the same way
 * SessionView.test.tsx does, and seeds the react-query cache so useSessionNeighbors
 * resolves to a known ordered list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { SessionView } from '../SessionView'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

function mk(id: string): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
  }
}

function wrapperWith(sessions: Session[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('SessionView prev/next chevrons', () => {
  beforeEach(() => window.localStorage.clear())

  it('renders prev/next chevrons with 44px touch targets when neighbors exist', () => {
    render(<SessionView sessionId="b" onBack={vi.fn()} onNavigateSession={vi.fn()} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    const next = screen.getByRole('button', { name: /next session/i })
    expect(screen.getByRole('button', { name: /previous session/i })).toBeInTheDocument()
    expect(next.className).toContain('min-h-[44px]')
  })

  it('navigates to the next session id when › is clicked', async () => {
    const user = userEvent.setup()
    const onNavigateSession = vi.fn()
    render(<SessionView sessionId="b" onBack={vi.fn()} onNavigateSession={onNavigateSession} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onNavigateSession).toHaveBeenCalledWith('c')
  })

  it('wraps to the first session when › is clicked on the last session', async () => {
    const user = userEvent.setup()
    const onNavigateSession = vi.fn()
    render(<SessionView sessionId="c" onBack={vi.fn()} onNavigateSession={onNavigateSession} />, {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onNavigateSession).toHaveBeenCalledWith('a')
  })

  it('hides the chevrons when there is only one session', () => {
    render(<SessionView sessionId="a" onBack={vi.fn()} onNavigateSession={vi.fn()} />, {
      wrapper: wrapperWith([mk('a')]),
    })
    expect(screen.queryByRole('button', { name: /next session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /previous session/i })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/session/__tests__/SessionView.neighbors.test.tsx`
Expected: FAIL — no button with name "Next session" (chevrons not implemented; `onNavigateSession` prop unknown to TS).

- [ ] **Step 3: Add the prop and the neighbors hook to SessionView**

In `web/src/components/session/SessionView.tsx`, add the import near the other `@/session` imports (after line 19):

```tsx
import { useSessionNeighbors } from '@/session/useSessionNeighbors'
```

Extend the Props interface (currently lines 37-41) to:

```tsx
interface Props {
  sessionId: string
  onBack: () => void
  onNavigateSession?: (id: string) => void
  initialPrompt?: string
}
```

Update the component signature (line 43) and add the hook call right after `rt` (after line 45):

```tsx
export function SessionView({ sessionId, onBack, onNavigateSession, initialPrompt }: Props) {
  const { detach } = useSessionSocket()
  const rt = useSessionRuntime(sessionId, initialPrompt)
  const { prevId, nextId } = useSessionNeighbors(sessionId)
```

- [ ] **Step 4: Render the chevrons in the top bar**

In the top bar JSX, the current order is: Back button → title `<span>` → `StatusPill` → `SessionActions`. Insert the prev chevron between the Back button and the title span, and the next chevron between the title span and the `StatusPill`. Replace the title `<span>` block (line 138) with the prev-chevron + title + next-chevron:

```tsx
        {(prevId !== null || nextId !== null) && (
          <button
            type="button"
            onClick={() => prevId && onNavigateSession?.(prevId)}
            aria-label="Previous session"
            className={cn(
              'flex items-center justify-center px-2 py-2 rounded-md min-h-[44px] min-w-[44px]',
              'text-lg leading-none font-medium text-gray-200',
              'hover:bg-gray-700 active:bg-gray-600',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            ‹
          </button>
        )}

        <span className="flex-1 text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

        {(prevId !== null || nextId !== null) && (
          <button
            type="button"
            onClick={() => nextId && onNavigateSession?.(nextId)}
            aria-label="Next session"
            className={cn(
              'flex items-center justify-center px-2 py-2 rounded-md min-h-[44px] min-w-[44px]',
              'text-lg leading-none font-medium text-gray-200',
              'hover:bg-gray-700 active:bg-gray-600',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            ›
          </button>
        )}
```

- [ ] **Step 5: Run the new test and the existing SessionView tests**

Run: `npx vitest run src/components/session/__tests__/SessionView.neighbors.test.tsx src/components/session/__tests__/SessionView.test.tsx`
Expected: PASS. (Existing `SessionView.test.tsx` renders with no seeded sessions → `useSessions` data is `undefined` → empty ordered list → null neighbors → chevrons hidden, so its assertions are unaffected.)

- [ ] **Step 6: Typecheck and lint**

Run: `npx tsc -b && npx eslint src/components/session/SessionView.tsx src/components/session/__tests__/SessionView.neighbors.test.tsx`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/session/__tests__/SessionView.neighbors.test.tsx
git commit -m "feat(web): prev/next session chevrons in mobile SessionView top bar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire `onNavigateSession` + remount in `App.tsx`

**Files:**
- Modify: `web/src/App.tsx` (`SessionRoute`, lines 51-65)
- Test: `web/src/App.test.tsx` (add a test; reuse existing harness)

**Interfaces:**
- Consumes: `SessionView`'s `onNavigateSession` prop (Task 2).
- Produces: `SessionRoute` renders `<SessionView key={sessionId} onNavigateSession={(id) => navigate('/session/${id}')} ... />`. The `key={sessionId}` forces a remount on session change so the websocket detaches the old session and attaches the new one via the existing runtime effects.

- [ ] **Step 1: Write the failing test**

Add this test inside the `describe('App routing', ...)` block in `web/src/App.test.tsx`. It overrides the `/sessions` mock to return two sessions, deep-links into the first, clicks Next, and asserts the URL advanced.

```tsx
  it('navigates to the next session via the › chevron', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/sessions') return Promise.resolve([
        { id: 'sess-1', name: 'One', accountName: 'alice', status: 'idle',
          createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
        { id: 'sess-2', name: 'Two', accountName: 'alice', status: 'idle',
          createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
      ])
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    })
    renderAt('/session/sess-1')
    // Wait for useSessions to populate so the chevrons appear.
    const next = await screen.findByRole('button', { name: /next session/i })
    await user.click(next)
    expect(screen.getByTestId('pathname')).toHaveTextContent('/session/sess-2')
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/App.test.tsx -t "next session via"`
Expected: FAIL — no "Next session" button (SessionRoute does not pass `onNavigateSession` yet).

- [ ] **Step 3: Wire the prop and key in SessionRoute**

In `web/src/App.tsx`, replace the `SessionView` element inside `SessionRoute` (lines 60-63) with:

```tsx
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <SessionView
        key={sessionId}
        sessionId={sessionId}
        onBack={() => navigate('/sessions')}
        onNavigateSession={(id) => navigate(`/session/${id}`)}
        initialPrompt={initialPrompt}
      />
    </div>
  )
```

- [ ] **Step 4: Run the new test plus the full App routing suite**

Run: `npx vitest run src/App.test.tsx`
Expected: PASS (existing routing tests still green — the single-session `MOCK_SESSIONS` deep-link test hides chevrons; the new test navigates).

- [ ] **Step 5: Typecheck, lint, and full web test suite**

Run: `npx tsc -b && npx eslint src/App.tsx && npx vitest run`
Expected: no type/lint errors; all tests pass.

- [ ] **Step 6: Commit**

```bash
git add web/src/App.tsx web/src/App.test.tsx
git commit -m "feat(web): wire mobile session prev/next navigation in SessionRoute

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- "Next = current list order" → Task 1 reuses `useSessionOrdering` (✓).
- "On-screen chevron arrows" → Task 2 (✓).
- "Wraparound at ends" → Task 1 modulo arithmetic + Task 2 wrap test (✓).
- "Hide when single / unknown session" → Task 1 null neighbors + Task 2 hide test (✓).
- "Re-attach on session change" → Task 3 `key={sessionId}` (✓).
- "Known limitation: full list, not search-filtered" → Task 1 calls `useSessionOrdering(sessions)` with no `matchedIds`, matching the spec (✓).
- Testing (unit hook + component + integration) → Tasks 1/2/3 (✓).

**Placeholder scan:** No TBD/TODO; every code step is complete.

**Type consistency:** `useSessionNeighbors` returns `{ prevId, nextId, index, total }` in Task 1 and is consumed with `{ prevId, nextId }` in Task 2; `onNavigateSession?: (id: string) => void` is defined in Task 2 and supplied in Task 3. Consistent.
