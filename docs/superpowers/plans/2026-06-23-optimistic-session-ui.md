# Optimistic Session UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make session create, archive, permanent-delete, unarchive (and their bulk variants) reflect in the UI instantly, reconciling with — or rolling back from — the server afterward, with failures announced via a new lightweight toast.

**Architecture:** All optimistic behavior lives in the React Query mutation hooks in `web/src/api/hooks.ts` (`onMutate`/`onError`/`onSuccess`/`onSettled`) so the four rendering sites (Dashboard, DesktopShell, ArchivedManager, SessionCard) need no per-component logic. Removals optimistically mutate the query cache and roll back on error. Create is special: PTY spin-up can outlast the 5s poll, so its placeholder lives in an out-of-cache module store (`pendingSessions`) that `useSessions` merges in, immune to background refetches. Failures surface through a new imperative, module-store-based toast (`lib/toast.ts`) — chosen over a context provider so the hooks can call `toast()` directly without wrapping every test in a provider.

**Tech Stack:** React 18 + TypeScript, `@tanstack/react-query` v5, Vite, Tailwind/shadcn, vitest + @testing-library/react. `useSyncExternalStore` for the two module stores.

## Global Constraints

- **Single source of truth (constitution VI):** one toast system, one pending-sessions store. Do not add a second toast/notification path; reuse `cn` from `@/lib/utils` and existing Tailwind tokens — no new design tokens.
- **TDD (constitution V):** every task writes a failing test first, then the minimal implementation.
- **Web test commands run inside `web/`** using the local binaries, never `npx`:
  - Unit/component: `./node_modules/.bin/vitest run <path>`
  - Real typecheck (the root `tsc --noEmit` checks nothing): `./node_modules/.bin/tsc -b --force`
  - Lint (CI uses `--max-warnings 0`): `./node_modules/.bin/eslint <path>`
- **node_modules** must be symlinked from the deploy worktree if absent (offline worktrees can't `npm ci`).
- **Client-only field:** `Session.pending` is never sent by the server and never serialized back to it.
- **React Query v5 callback order:** hook-level `onMutate → mutationFn → onSuccess|onError → onSettled`; the context object returned by `onMutate` is passed to `onError`/`onSuccess`/`onSettled`. Per-`mutate()`-call callbacks in components still fire *in addition* to hook-level ones — do not remove the existing component `onSuccess`/`onError` handlers.

---

## File Structure

- **Create** `web/src/lib/toast.ts` — module store: `toast()`, `dismissToast()`, `subscribe()`, `getToasts()`, types, test reset helper.
- **Create** `web/src/lib/toast.test.ts` — store unit tests.
- **Create** `web/src/components/ToastViewport.tsx` — subscribes via `useSyncExternalStore`, renders the stack.
- **Create** `web/src/components/__tests__/ToastViewport.test.tsx` — render/dismiss/action tests.
- **Create** `web/src/lib/pendingSessions.ts` — module store for optimistic create placeholders.
- **Create** `web/src/lib/pendingSessions.test.ts` — store unit tests.
- **Modify** `web/src/api/types.ts` — add `Session.pending?: boolean`.
- **Modify** `web/src/api/hooks.ts` — merge pending into `useSessions`; add optimistic logic to the six mutation hooks; add a private `unarchiveRequest` helper.
- **Modify** `web/src/api/hooks.test.ts` (or new sibling test files) — optimistic-behavior tests per hook.
- **Modify** `web/src/components/SessionCard.tsx` — render a non-interactive "Starting…" variant when `session.pending`.
- **Modify** `web/src/components/__tests__/` — a SessionCard pending-render test.
- **Modify** `web/src/App.tsx` — mount `<ToastViewport />` once.

---

## Task 1: Toast module store

**Files:**
- Create: `web/src/lib/toast.ts`
- Test: `web/src/lib/toast.test.ts`

**Interfaces:**
- Produces:
  - `type ToastVariant = 'info' | 'success' | 'error'`
  - `interface ToastAction { label: string; onClick: () => void }`
  - `interface ToastItem { id: number; message: string; variant: ToastVariant; action?: ToastAction }`
  - `function toast(input: { message: string; variant?: ToastVariant; action?: ToastAction; durationMs?: number }): number`
  - `function dismissToast(id: number): void`
  - `function subscribe(listener: () => void): () => void`
  - `function getToasts(): ToastItem[]`
  - `function __resetToasts(): void` (test-only)

- [ ] **Step 1: Write the failing test**

```ts
// web/src/lib/toast.test.ts
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { toast, dismissToast, getToasts, subscribe, __resetToasts } from './toast'

describe('toast store', () => {
  beforeEach(() => { __resetToasts(); vi.useRealTimers() })

  it('adds a toast and notifies subscribers', () => {
    const seen: number[] = []
    subscribe(() => seen.push(getToasts().length))
    const id = toast({ message: 'hi' })
    expect(getToasts()).toHaveLength(1)
    expect(getToasts()[0]).toMatchObject({ id, message: 'hi', variant: 'info' })
    expect(seen).toEqual([1])
  })

  it('dismiss removes by id', () => {
    const id = toast({ message: 'a' })
    toast({ message: 'b' })
    dismissToast(id)
    expect(getToasts().map((t) => t.message)).toEqual(['b'])
  })

  it('auto-dismisses after the default duration', () => {
    vi.useFakeTimers()
    toast({ message: 'temp' })
    expect(getToasts()).toHaveLength(1)
    vi.advanceTimersByTime(5000)
    expect(getToasts()).toHaveLength(0)
  })

  it('keeps a snapshot reference stable between changes', () => {
    toast({ message: 'x' })
    expect(getToasts()).toBe(getToasts())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/toast.test.ts`
Expected: FAIL — `Cannot find module './toast'`.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/lib/toast.ts
export type ToastVariant = 'info' | 'success' | 'error'

export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastItem {
  id: number
  message: string
  variant: ToastVariant
  action?: ToastAction
}

const DEFAULT_MS = 5000
const ACTION_MS = 8000

let toasts: ToastItem[] = []
let nextId = 1
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// Stable reference between mutations — required by useSyncExternalStore.
export function getToasts(): ToastItem[] {
  return toasts
}

export function dismissToast(id: number): void {
  const next = toasts.filter((t) => t.id !== id)
  if (next.length !== toasts.length) {
    toasts = next
    emit()
  }
}

export function toast(input: {
  message: string
  variant?: ToastVariant
  action?: ToastAction
  durationMs?: number
}): number {
  const id = nextId++
  toasts = [...toasts, { id, message: input.message, variant: input.variant ?? 'info', action: input.action }]
  emit()
  const ms = input.durationMs ?? (input.action ? ACTION_MS : DEFAULT_MS)
  if (ms > 0 && typeof setTimeout !== 'undefined') {
    setTimeout(() => dismissToast(id), ms)
  }
  return id
}

/** Test-only: clear all state between tests. */
export function __resetToasts(): void {
  toasts = []
  nextId = 1
  listeners.clear()
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/toast.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + commit**

```bash
cd web && ./node_modules/.bin/eslint src/lib/toast.ts src/lib/toast.test.ts
git add web/src/lib/toast.ts web/src/lib/toast.test.ts
git commit -m "feat(web): add lightweight toast module store"
```

---

## Task 2: ToastViewport component + mount in App

**Files:**
- Create: `web/src/components/ToastViewport.tsx`
- Test: `web/src/components/__tests__/ToastViewport.test.tsx`
- Modify: `web/src/App.tsx` (the `App` function, currently lines 86–107)

**Interfaces:**
- Consumes: `subscribe`, `getToasts`, `dismissToast`, `ToastItem` from `@/lib/toast` (Task 1).
- Produces: `export function ToastViewport(): JSX.Element | null`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/ToastViewport.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ToastViewport } from '../ToastViewport'
import { toast, getToasts, __resetToasts } from '@/lib/toast'
import { act } from 'react'

describe('ToastViewport', () => {
  beforeEach(() => { __resetToasts() })

  it('renders nothing when empty', () => {
    const { container } = render(<ToastViewport />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a toast message and dismisses on the × button', () => {
    render(<ToastViewport />)
    act(() => { toast({ message: 'Archived Foo', variant: 'success' }) })
    expect(screen.getByText('Archived Foo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText('Archived Foo')).not.toBeInTheDocument()
  })

  it('fires the action callback and dismisses', () => {
    const onClick = vi.fn()
    render(<ToastViewport />)
    act(() => { toast({ message: 'Archived Foo', action: { label: 'Undo', onClick } }) })
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(getToasts()).toHaveLength(0)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/ToastViewport.test.tsx`
Expected: FAIL — `Cannot find module '../ToastViewport'`.

- [ ] **Step 3: Write the component**

```tsx
// web/src/components/ToastViewport.tsx
import { useSyncExternalStore } from 'react'
import { subscribe, getToasts, dismissToast, type ToastItem } from '@/lib/toast'
import { cn } from '@/lib/utils'

const variantClass: Record<ToastItem['variant'], string> = {
  info: 'bg-gray-800 text-gray-50 dark:bg-gray-700',
  success: 'bg-gray-800 text-gray-50 dark:bg-gray-700',
  error: 'bg-red-600 text-white',
}

export function ToastViewport() {
  const toasts = useSyncExternalStore(subscribe, getToasts, getToasts)
  if (toasts.length === 0) return null
  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-[calc(100vw-2rem)]"
      role="region"
      aria-label="Notifications"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          aria-live="polite"
          className={cn('flex items-center gap-3 rounded-md px-4 py-3 text-sm shadow-lg', variantClass[t.variant])}
        >
          <span className="flex-1">{t.message}</span>
          {t.action && (
            <button
              type="button"
              onClick={() => { t.action!.onClick(); dismissToast(t.id) }}
              className="font-medium underline underline-offset-2 hover:opacity-80"
            >
              {t.action.label}
            </button>
          )}
          <button
            type="button"
            aria-label="Dismiss notification"
            onClick={() => dismissToast(t.id)}
            className="opacity-70 hover:opacity-100"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/ToastViewport.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Mount in App**

In `web/src/App.tsx`, add the import near the other component imports:

```tsx
import { ToastViewport } from '@/components/ToastViewport'
```

Replace the `App` function body (currently lines 86–107) with a version that always renders the viewport:

```tsx
function App() {
  // Wide screens get the master-detail DesktopShell; narrow screens keep the
  // routed full-screen views below. useMediaQuery returns false when matchMedia
  // is unavailable (jsdom), so tests default to the mobile/routed branch.
  const isWide = useMediaQuery('(min-width: 1024px)')
  return (
    <>
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
          <Route path="/accounts" element={<DashboardRoute tab="accounts" />} />
          <Route path="/archived" element={<DashboardRoute tab="archived" />} />
          <Route path="/session/:sessionId" element={<SessionRoute />} />
          <Route path="/session/:sessionId/subagent/:agentId" element={<SubSessionRoute />} />
          <Route path="/review/:sessionId" element={<ReviewRoute />} />
          <Route path="*" element={<Navigate to="/sessions" replace />} />
        </Routes>
      )}
      <ToastViewport />
    </>
  )
}
```

- [ ] **Step 6: Verify App tests still pass, typecheck, lint, commit**

Run: `cd web && ./node_modules/.bin/vitest run src/App.test.tsx src/App.dialogs.test.tsx && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/components/ToastViewport.tsx src/App.tsx`
Expected: PASS, no type errors, no lint warnings.

```bash
git add web/src/components/ToastViewport.tsx web/src/components/__tests__/ToastViewport.test.tsx web/src/App.tsx
git commit -m "feat(web): mount toast viewport at app root"
```

---

## Task 3: `Session.pending` field + pendingSessions store + merge into useSessions

**Files:**
- Modify: `web/src/api/types.ts` (the `Session` interface, lines 53–78)
- Create: `web/src/lib/pendingSessions.ts`
- Create: `web/src/lib/pendingSessions.test.ts`
- Modify: `web/src/api/hooks.ts` (`useSessions`, lines 40–47; add imports)
- Test: `web/src/api/hooks.useSessions.test.tsx` (new)

**Interfaces:**
- Produces:
  - `Session.pending?: boolean` (client-only).
  - `function addPendingSession(s: Omit<Session, 'id' | 'pending'>): string` — returns the temp id (`pending-<n>`).
  - `function removePendingSession(id: string): void`
  - `function getPendingSessions(): Session[]`
  - `function subscribePending(listener: () => void): () => void`
  - `function __resetPending(): void` (test-only)
  - `useSessions()` now returns the query result with `data` = server sessions concatenated with pending placeholders.

- [ ] **Step 1: Add the client-only field**

In `web/src/api/types.ts`, inside the `Session` interface, after the `pr?` field (line 77), add:

```ts
  /** Client-only: true while an optimistic create placeholder awaits the server. Never sent by the server. */
  pending?: boolean
```

- [ ] **Step 2: Write the failing store test**

```ts
// web/src/lib/pendingSessions.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { addPendingSession, removePendingSession, getPendingSessions, __resetPending } from './pendingSessions'

describe('pendingSessions store', () => {
  beforeEach(() => { __resetPending() })

  it('adds a placeholder with a temp id and pending flag', () => {
    const id = addPendingSession({ name: 'New', accountName: '', status: 'in_progress', createdAt: '2026-06-23T00:00:00Z' })
    expect(id).toMatch(/^pending-/)
    const list = getPendingSessions()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id, name: 'New', pending: true })
  })

  it('removes by id', () => {
    const id = addPendingSession({ name: 'A', accountName: '', status: 'in_progress', createdAt: 'x' })
    removePendingSession(id)
    expect(getPendingSessions()).toHaveLength(0)
  })

  it('keeps a stable snapshot reference between changes', () => {
    addPendingSession({ name: 'A', accountName: '', status: 'in_progress', createdAt: 'x' })
    expect(getPendingSessions()).toBe(getPendingSessions())
  })
})
```

- [ ] **Step 3: Run it to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/pendingSessions.test.ts`
Expected: FAIL — `Cannot find module './pendingSessions'`.

- [ ] **Step 4: Write the store**

```ts
// web/src/lib/pendingSessions.ts
import type { Session } from '@/api/types'

let pending: Session[] = []
let nextTempId = 1
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function subscribePending(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// Stable reference between mutations — required by useSyncExternalStore.
export function getPendingSessions(): Session[] {
  return pending
}

export function addPendingSession(s: Omit<Session, 'id' | 'pending'>): string {
  const id = `pending-${nextTempId++}`
  pending = [...pending, { ...s, id, pending: true }]
  emit()
  return id
}

export function removePendingSession(id: string): void {
  const next = pending.filter((s) => s.id !== id)
  if (next.length !== pending.length) {
    pending = next
    emit()
  }
}

/** Test-only: clear all state between tests. */
export function __resetPending(): void {
  pending = []
  nextTempId = 1
  listeners.clear()
}
```

- [ ] **Step 5: Run store test to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/pendingSessions.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Write the failing useSessions merge test**

```tsx
// web/src/api/hooks.useSessions.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSessions } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { addPendingSession, __resetPending } from '@/lib/pendingSessions'

vi.mock('./client', () => ({ api: { get: vi.fn() }, ApiRequestError: class extends Error {} }))

const real: Session = { id: 'sess-1', name: 'Real', accountName: 'a', status: 'idle', createdAt: 'x' }

describe('useSessions — merges pending placeholders', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks(); __resetPending()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('appends pending placeholders to the server list', async () => {
    vi.mocked(api.get).mockResolvedValue([real])
    const { result } = renderHook(() => useSessions(), { wrapper })
    await waitFor(() => expect(result.current.data).toEqual([real]))

    act(() => { addPendingSession({ name: 'Pending', accountName: '', status: 'in_progress', createdAt: 'y' }) })
    await waitFor(() => {
      expect(result.current.data).toHaveLength(2)
      expect(result.current.data?.some((s) => s.pending)).toBe(true)
    })
  })
})
```

- [ ] **Step 7: Run it to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.useSessions.test.tsx`
Expected: FAIL — pending placeholder is not present in `result.current.data` (only `[real]`).

- [ ] **Step 8: Implement the merge in useSessions**

In `web/src/api/hooks.ts`, add imports at the top (after the existing imports):

```ts
import { useMemo, useSyncExternalStore } from 'react'
import { getPendingSessions, subscribePending } from '@/lib/pendingSessions'
```

Replace `useSessions` (lines 40–47) with:

```ts
export function useSessions() {
  const pending = useSyncExternalStore(subscribePending, getPendingSessions, getPendingSessions)
  const query = useQuery<Session[]>({
    queryKey: QK.sessions,
    queryFn: () => api.get<Session[]>('/sessions'),
    staleTime: 3_000,
    refetchInterval: 5_000,
  })
  const data = useMemo<Session[] | undefined>(() => {
    if (pending.length === 0) return query.data
    return [...(query.data ?? []), ...pending]
  }, [query.data, pending])
  return { ...query, data }
}
```

- [ ] **Step 9: Run merge test + typecheck**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.useSessions.test.tsx && ./node_modules/.bin/tsc -b --force`
Expected: PASS. If `tsc -b` objects to overriding `data` on the union result, annotate the return as `as typeof query` is NOT allowed (data type differs) — instead leave the explicit `Session[] | undefined` on `data` (already done) which is assignable; the spread keeps `isLoading`/`isError` intact.

- [ ] **Step 10: Lint + commit**

```bash
cd web && ./node_modules/.bin/eslint src/lib/pendingSessions.ts src/lib/pendingSessions.test.ts src/api/hooks.ts src/api/hooks.useSessions.test.tsx
git add web/src/api/types.ts web/src/lib/pendingSessions.ts web/src/lib/pendingSessions.test.ts web/src/api/hooks.ts web/src/api/hooks.useSessions.test.tsx
git commit -m "feat(web): merge optimistic pending sessions into useSessions"
```

---

## Task 4: SessionCard pending ("Starting…") rendering

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (the `SessionCard` function, starts line 34)
- Test: `web/src/components/__tests__/SessionCardPending.test.tsx` (new)

**Interfaces:**
- Consumes: `Session.pending` (Task 3). `SessionCardProps` is unchanged: `{ session, account, selected?, onOpen, onRename, onClose, onDelete, onQuickResume }`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/SessionCardPending.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: 'pending-1', name: 'My new session', accountName: '', status: 'in_progress',
  createdAt: '2026-06-23T00:00:00Z', pending: true,
}

describe('SessionCard pending placeholder', () => {
  it('shows a Starting… label and does not fire onOpen when clicked', () => {
    const onOpen = vi.fn()
    render(<SessionCard session={base} account={undefined} onOpen={onOpen} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />)
    expect(screen.getByText('My new session')).toBeInTheDocument()
    expect(screen.getByText(/starting/i)).toBeInTheDocument()
    fireEvent.click(screen.getByText('My new session'))
    expect(onOpen).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run it to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/SessionCardPending.test.tsx`
Expected: FAIL — no "Starting…" text, or `onOpen` is called.

- [ ] **Step 3: Add the pending branch (AFTER the hooks)**

`cn` is already imported in `SessionCard.tsx`. The pending branch must be placed **after all hook calls** — directly before the main `return (` on line 64 (i.e. after `handleKeyDown` is defined on line 62). Placing it before `useRef`/`useState`/`useEffect` would trip `react-hooks/rules-of-hooks` and fail CI's `--max-warnings 0`. Running those hooks harmlessly for a placeholder is fine; a placeholder (`pending-N` id) and its eventual real card (real id) are separate keyed instances in `SessionList`, so a single instance never flips `pending` and the extra hook calls are inert.

The outer `className` reuses the real card's container classes (copied verbatim from the existing `<article>` on lines 67–72) plus `opacity-70 cursor-default select-none`:

```tsx
  if (session.pending) {
    return (
      <article
        data-testid="session-card-pending"
        aria-busy="true"
        className={cn(
          'group flex flex-col rounded-lg border border-gray-200 dark:border-gray-700',
          'bg-white dark:bg-gray-800 px-3 py-2 min-h-[44px]',
          'opacity-70 cursor-default select-none',
        )}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="inline-block h-3 w-3 flex-shrink-0 animate-spin rounded-full border-2 border-gray-400 border-t-transparent"
            aria-hidden="true"
          />
          <span className="flex-1 min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-100">
            {session.name}
          </span>
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Starting…</p>
      </article>
    )
  }
```

- [ ] **Step 4: Run the test to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/SessionCardPending.test.tsx`
Expected: PASS.

- [ ] **Step 5: Run the existing SessionCard/list tests to confirm no regressions, typecheck, lint, commit**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/SessionActions.test.tsx && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/components/SessionCard.tsx src/components/__tests__/SessionCardPending.test.tsx`
Expected: PASS / clean.

```bash
git add web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCardPending.test.tsx
git commit -m "feat(web): render Starting… placeholder for pending sessions"
```

---

## Task 5: Optimistic create

**Files:**
- Modify: `web/src/api/hooks.ts` (`useCreateSession`, lines 128–142; add import)
- Test: `web/src/api/hooks.createSession.test.tsx` (extend the existing file)

**Interfaces:**
- Consumes: `addPendingSession`, `removePendingSession` (Task 3).
- Produces: `useCreateSession` now inserts a pending placeholder on `mutate`, swaps in the real session on success, and removes the placeholder on settle. The existing `onSuccess` cache insert (dedup by id) is preserved.

**No component change required for "dialog stays open":** this is already the behavior. Mobile `NewSessionDialog` (`Dashboard.tsx`) only dismisses in its per-call `onSuccess` and keeps itself open showing `externalError`/`branchError` on failure; the desktop `DispatchBar` disables its inputs via `isPending` while the mutation runs. The placeholder added here renders in the list *behind* the open dialog, so the dashboard already feels responsive. Do not remove or alter those component-level `onSuccess`/`onError` handlers — the hook-level callbacks run in addition to them.

- [ ] **Step 1: Write the failing tests (append to the existing describe file)**

```tsx
// append to web/src/api/hooks.createSession.test.tsx
import { getPendingSessions, __resetPending } from '@/lib/pendingSessions'

describe('useCreateSession — optimistic placeholder', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetPending(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('adds a pending placeholder immediately and removes it after success', async () => {
    qc.setQueryData(QK.sessions, [] as Session[])
    let resolve!: (s: Session) => void
    vi.mocked(api.post).mockReturnValue(new Promise<Session>((r) => { resolve = r }))

    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ name: 'Hello', initialPrompt: 'go' })

    await waitFor(() => expect(getPendingSessions()).toHaveLength(1))
    expect(getPendingSessions()[0]).toMatchObject({ name: 'Hello', pending: true })

    resolve(created)
    await waitFor(() => {
      expect(getPendingSessions()).toHaveLength(0)
      expect(qc.getQueryData<Session[]>(QK.sessions)).toContainEqual(created)
    })
  })

  it('removes the placeholder on failure (no rollback drama on the list)', async () => {
    qc.setQueryData(QK.sessions, [] as Session[])
    vi.mocked(api.post).mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ name: 'Hello', initialPrompt: 'go' })
    await waitFor(() => expect(getPendingSessions()).toHaveLength(0))
    expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([])
  })
})
```

(Note: `_a` import is only to guarantee the module is loaded; if eslint flags it as unused, import only `getPendingSessions, __resetPending`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.createSession.test.tsx`
Expected: FAIL — placeholder never added (no `onMutate`).

- [ ] **Step 3: Implement optimistic create**

In `web/src/api/hooks.ts`, extend the pendingSessions import to include the mutators:

```ts
import { getPendingSessions, subscribePending, addPendingSession, removePendingSession } from '@/lib/pendingSessions'
```

Replace `useCreateSession` (lines 128–142) with:

```ts
export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput, { tempId: string }>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    onMutate: (input) => {
      // Placeholder lives outside the query cache so the 5s poll can't wipe it
      // while the backend spins up the claude PTY.
      const tempId = addPendingSession({
        name: input.name?.trim() || input.initialPrompt?.trim().slice(0, 60) || 'New session',
        accountName: input.account ?? '',
        status: 'in_progress',
        createdAt: new Date().toISOString(),
        repo: input.repo ?? null,
      })
      return { tempId }
    },
    onSuccess: (session) => {
      // Inject the real card before the placeholder is removed (onSettled) to avoid a flicker.
      qc.setQueryData<Session[]>(QK.sessions, (prev) => {
        const list = prev ?? []
        if (list.some((s) => s.id === session.id)) return list
        return [...list, session]
      })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
    onSettled: (_data, _err, _input, ctx) => {
      if (ctx?.tempId) removePendingSession(ctx.tempId)
    },
  })
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.createSession.test.tsx`
Expected: PASS (all create tests, old and new).

- [ ] **Step 5: Typecheck, lint, commit**

Run: `cd web && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/api/hooks.ts src/api/hooks.createSession.test.tsx`

```bash
git add web/src/api/hooks.ts web/src/api/hooks.createSession.test.tsx
git commit -m "feat(web): optimistic create placeholder via pendingSessions"
```

---

## Task 6: Optimistic archive (useCloseSession) with Undo

**Files:**
- Modify: `web/src/api/hooks.ts` (`useCloseSession`, lines 160–169; add a private `unarchiveRequest` helper; import `toast`)
- Test: `web/src/api/hooks.archive.test.tsx` (new)

**Interfaces:**
- Consumes: `toast` from `@/lib/toast`.
- Produces:
  - `function unarchiveRequest(id: string): Promise<ArchivedSession>` (module-private; reused by `useUnarchiveSession` in Task 7 and by the Undo action here).
  - `useCloseSession` removes the session from `QK.sessions` on `mutate`, rolls back + error-toasts on failure, success-toasts with an Undo action.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/api/hooks.archive.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useCloseSession, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))

const s: Session = { id: 'sess-1', name: 'Foo', accountName: 'a', status: 'idle', createdAt: 'x' }

describe('useCloseSession — optimistic archive', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes the session immediately and shows an Undo toast on success', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    // optimistic removal is synchronous in onMutate
    await waitFor(() => expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([]))
    await waitFor(() => {
      const t = getToasts()
      expect(t).toHaveLength(1)
      expect(t[0]).toMatchObject({ message: 'Archived Foo', variant: 'success' })
      expect(t[0].action?.label).toBe('Undo')
    })
  })

  it('rolls back and shows an error toast on failure', async () => {
    qc.setQueryData(QK.sessions, [s])
    vi.mocked(api.del).mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => useCloseSession(), { wrapper })
    result.current.mutate('sess-1')
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([s])
      expect(getToasts()[0]).toMatchObject({ message: "Couldn't archive Foo", variant: 'error' })
    })
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.archive.test.tsx`
Expected: FAIL — session not removed synchronously / no toast.

- [ ] **Step 3: Implement**

In `web/src/api/hooks.ts`, add the toast import:

```ts
import { toast } from '@/lib/toast'
```

Add the private helper above `useUnarchiveSession` (near the archived hooks, ~line 263):

```ts
// Single source of truth for the unarchive endpoint — reused by useUnarchiveSession
// and the archive Undo action.
function unarchiveRequest(id: string) {
  return api.post<ArchivedSession>(`/sessions/${id}/unarchive`)
}
```

Replace `useCloseSession` (lines 160–169) with:

```ts
export function useCloseSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string, { prev?: Session[]; removed?: Session }>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}`),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.sessions })
      const prev = qc.getQueryData<Session[]>(QK.sessions)
      const removed = prev?.find((x) => x.id === id)
      qc.setQueryData<Session[]>(QK.sessions, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prev, removed }
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.sessions, ctx.prev)
      toast({ message: `Couldn't archive ${ctx?.removed?.name ?? 'session'}`, variant: 'error' })
    },
    onSuccess: (_data, id, ctx) => {
      toast({
        message: `Archived ${ctx?.removed?.name ?? 'session'}`,
        variant: 'success',
        action: {
          label: 'Undo',
          onClick: () => {
            void unarchiveRequest(id).then(() => {
              qc.invalidateQueries({ queryKey: QK.sessions })
              qc.invalidateQueries({ queryKey: QK.archived })
            })
          },
        },
      })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.archive.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Typecheck, lint, commit**

Run: `cd web && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/api/hooks.ts src/api/hooks.archive.test.tsx`

```bash
git add web/src/api/hooks.ts web/src/api/hooks.archive.test.tsx
git commit -m "feat(web): optimistic archive with rollback and Undo toast"
```

---

## Task 7: Optimistic permanent-delete and unarchive

**Files:**
- Modify: `web/src/api/hooks.ts` (`useUnarchiveSession` lines 264–273, `usePermanentDeleteSession` lines 275–286)
- Test: `web/src/api/hooks.delete.test.tsx` (new)

**Interfaces:**
- Consumes: `unarchiveRequest` (Task 6), `toast`.
- Produces:
  - `usePermanentDeleteSession`: removes from both `QK.archived` and `QK.sessions` on mutate (a live card can be permanently deleted too), rolls back + error-toasts, success-toasts (no Undo).
  - `useUnarchiveSession`: uses `unarchiveRequest`, optimistically removes from `QK.archived`, rolls back + error-toasts on failure.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/api/hooks.delete.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { usePermanentDeleteSession, useUnarchiveSession, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))

const arch: ArchivedSession = { id: 'a-1', name: 'Bar', account: 'a', cwd: '/x', claudeSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' }

describe('usePermanentDeleteSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately and toasts on success', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(getToasts()[0]).toMatchObject({ message: 'Deleted Bar' }))
  })

  it('rolls back and error-toasts on failure', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockRejectedValue(new Error('no'))
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([arch])
      expect(getToasts()[0]).toMatchObject({ message: "Couldn't delete Bar", variant: 'error' })
    })
  })
})

describe('useUnarchiveSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately on mutate', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.post).mockResolvedValue(arch)
    const { result } = renderHook(() => useUnarchiveSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.delete.test.tsx`
Expected: FAIL — items not removed synchronously / no toasts.

- [ ] **Step 3: Implement both hooks**

Replace `useUnarchiveSession` (lines 264–273) with:

```ts
export function useUnarchiveSession() {
  const qc = useQueryClient()
  return useMutation<ArchivedSession, ApiRequestError, string, { prev?: ArchivedSession[]; removed?: ArchivedSession }>({
    mutationFn: (id) => unarchiveRequest(id),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      const prev = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const removed = prev?.find((x) => x.id === id)
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prev, removed }
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      toast({ message: `Couldn't restore ${ctx?.removed?.name ?? 'session'}`, variant: 'error' })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}
```

Replace `usePermanentDeleteSession` (lines 275–286) with:

```ts
export function usePermanentDeleteSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string, { prevArchived?: ArchivedSession[]; prevSessions?: Session[]; name?: string }>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}/permanent`),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      await qc.cancelQueries({ queryKey: QK.sessions })
      const prevArchived = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const prevSessions = qc.getQueryData<Session[]>(QK.sessions)
      const name = prevArchived?.find((x) => x.id === id)?.name ?? prevSessions?.find((x) => x.id === id)?.name
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => x.id !== id))
      qc.setQueryData<Session[]>(QK.sessions, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prevArchived, prevSessions, name }
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prevArchived) qc.setQueryData(QK.archived, ctx.prevArchived)
      if (ctx?.prevSessions) qc.setQueryData(QK.sessions, ctx.prevSessions)
      toast({ message: `Couldn't delete ${ctx?.name ?? 'session'}`, variant: 'error' })
    },
    onSuccess: (_d, _id, ctx) => {
      toast({ message: `Deleted ${ctx?.name ?? 'session'}`, variant: 'success' })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.archived })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.delete.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Typecheck, lint, commit**

Run: `cd web && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/api/hooks.ts src/api/hooks.delete.test.tsx`

```bash
git add web/src/api/hooks.ts web/src/api/hooks.delete.test.tsx
git commit -m "feat(web): optimistic permanent-delete and unarchive with toasts"
```

---

## Task 8: Optimistic bulk unarchive + bulk permanent-delete

**Files:**
- Modify: `web/src/api/hooks.ts` (`useBulkUnarchiveSessions` lines 288–297, `useBulkPermanentDeleteSessions` lines 299–309)
- Test: `web/src/api/hooks.bulk.test.tsx` (new)

**Interfaces:**
- Consumes: `toast`, `BatchResult` (already imported).
- Produces: both bulk hooks optimistically remove all targeted ids from `QK.archived` on mutate; on success they toast a summary (`Deleted N` / `Deleted N · M failed`); failed ids reappear via the `onSettled` invalidate; whole-request failure rolls back all and error-toasts.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/api/hooks.bulk.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useBulkPermanentDeleteSessions, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession, BatchResult } from './types'
import { getToasts, __resetToasts } from '@/lib/toast'

vi.mock('./client', () => ({ api: { post: vi.fn() }, ApiRequestError: class extends Error {} }))

const mk = (id: string): ArchivedSession => ({ id, name: id, account: 'a', cwd: '/x', claudeSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' })

describe('useBulkPermanentDeleteSessions — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); __resetToasts(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes all targeted ids immediately and summarizes partial failure', async () => {
    qc.setQueryData(QK.archived, [mk('a'), mk('b'), mk('c')])
    const result1: BatchResult = { succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] }
    vi.mocked(api.post).mockResolvedValue(result1)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(getToasts()[0]).toMatchObject({ message: 'Deleted 2 · 1 failed', variant: 'error' }))
  })

  it('rolls back all on whole-request failure', async () => {
    const initial = [mk('a'), mk('b')]
    qc.setQueryData(QK.archived, initial)
    vi.mocked(api.post).mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b'])
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual(initial)
      expect(getToasts()[0]).toMatchObject({ variant: 'error' })
    })
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.bulk.test.tsx`
Expected: FAIL — ids not removed / no summary toast.

- [ ] **Step 3: Implement both bulk hooks**

Replace `useBulkUnarchiveSessions` (lines 288–297) with:

```ts
export function useBulkUnarchiveSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[], { prev?: ArchivedSession[] }>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/unarchive', { ids }),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      const prev = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const idSet = new Set(ids)
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => !idSet.has(x.id)))
      return { prev }
    },
    onError: (_e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      toast({ message: `Couldn't restore sessions`, variant: 'error' })
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      toast({ message: f > 0 ? `Restored ${n} · ${f} failed` : `Restored ${n}`, variant: f > 0 ? 'error' : 'success' })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}
```

Replace `useBulkPermanentDeleteSessions` (lines 299–309) with:

```ts
export function useBulkPermanentDeleteSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[], { prev?: ArchivedSession[] }>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/permanent-delete', { ids }),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      const prev = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const idSet = new Set(ids)
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => !idSet.has(x.id)))
      return { prev }
    },
    onError: (_e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      toast({ message: `Couldn't delete sessions`, variant: 'error' })
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      toast({ message: f > 0 ? `Deleted ${n} · ${f} failed` : `Deleted ${n}`, variant: f > 0 ? 'error' : 'success' })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.archived })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && ./node_modules/.bin/vitest run src/api/hooks.bulk.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Full web gate, then commit**

Run the full suite the way CI does (the frontend job runs the whole vitest suite, not a subset):

```bash
cd web && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint . --max-warnings 0 && ./node_modules/.bin/vitest run
```
Expected: typecheck clean, no lint warnings, all tests pass. Investigate any pre-existing CI-only suites (react-router-dom/@novnc) per the worktree notes if they fail only offline.

```bash
git add web/src/api/hooks.ts web/src/api/hooks.bulk.test.tsx
git commit -m "feat(web): optimistic bulk unarchive and permanent-delete with summary toasts"
```

---

## Final verification (before opening the PR)

- [ ] `cd web && ./node_modules/.bin/tsc -b --force` — no type errors.
- [ ] `cd web && ./node_modules/.bin/eslint . --max-warnings 0` — clean.
- [ ] `cd web && ./node_modules/.bin/vitest run` — all green.
- [ ] Manual smoke (optional, via the `run` skill): create a session → placeholder "Starting…" card appears instantly, dialog/bar stays in its pending state, card reconciles on success; archive a session → it vanishes immediately with an "Archived · Undo" toast, Undo restores it; permanent-delete from the archived tab → row vanishes immediately; force a failure (e.g. stop the backend) → the item snaps back with an error toast.

## Notes on scope and rejected complexity (YAGNI)

- No tombstone set for removal races: `cancelQueries` + `onSettled` invalidate covers the sub-5s window; add only if flicker is observed.
- No Undo on permanent-delete or bulk ops (irreversible / multi-target); archive-only Undo per the spec.
- Account/adoption mutations are untouched.
