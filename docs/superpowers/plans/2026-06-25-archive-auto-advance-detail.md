# Auto-advance the detail view on archive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the session you're viewing is archived (or deleted), the detail view advances to the next visible session instead of dead-ending on the list / welcome pane.

**Architecture:** One pure helper (`nextVisibleSessionId`) computes the advance target from the current ordered id list. Both hosts — desktop (`DesktopShell`, local selection state) and mobile (`SessionRoute` → `SessionView`, router navigation) — call it from their existing archive/delete callbacks, capturing the pre-archive ordered list in the click-time closure so the optimistic cache removal can't race it. `SessionActions` is untouched.

**Tech Stack:** React + TypeScript, react-router-dom, TanStack Query, Vitest + Testing Library.

## Global Constraints

- Frontend only — no backend / API / model changes.
- Run web checks from `web/`: `vitest run`, `npx tsc -b --force` (the real type gate; `tsc --noEmit` checks nothing here), and `eslint` with `--max-warnings 0`.
- Don't touch `useCloseSession` optimistic removal, the Undo toast, or error-revert behavior.
- "Next" semantics (locked in design): the row **below** the removed one; if it was **last**, the row **above**; if it was the **only** row (or not in the visible list), `null` → fall back to list (mobile) / welcome pane (desktop).
- Spec: `docs/superpowers/specs/2026-06-25-archive-auto-advance-detail-design.md`.

---

### Task 1: Pure advance-target helper

**Files:**
- Create: `web/src/session/nextAfterArchive.ts`
- Test: `web/src/session/nextAfterArchive.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `nextVisibleSessionId(orderedIds: readonly string[], removedId: string): string | null` — the id of the session the detail view should advance to, or `null` when the caller should fall back to the list/welcome.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/nextAfterArchive.test.ts
import { describe, it, expect } from 'vitest'
import { nextVisibleSessionId } from './nextAfterArchive'

describe('nextVisibleSessionId', () => {
  const ids = ['a', 'b', 'c']

  it('returns the row below a middle row', () => {
    expect(nextVisibleSessionId(ids, 'b')).toBe('c')
  })

  it('returns the row below the first row', () => {
    expect(nextVisibleSessionId(ids, 'a')).toBe('b')
  })

  it('returns the row above when removing the last row', () => {
    expect(nextVisibleSessionId(ids, 'c')).toBe('b')
  })

  it('returns null when removing the only row', () => {
    expect(nextVisibleSessionId(['solo'], 'solo')).toBeNull()
  })

  it('returns null when the removed id is not in the visible list', () => {
    expect(nextVisibleSessionId(ids, 'zzz')).toBeNull()
  })

  it('returns null for an empty list', () => {
    expect(nextVisibleSessionId([], 'a')).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/session/nextAfterArchive.test.ts`
Expected: FAIL — cannot resolve `./nextAfterArchive` / `nextVisibleSessionId is not a function`.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/session/nextAfterArchive.ts
/**
 * Pick the session the detail view should advance to when `removedId` leaves the
 * live list. `orderedIds` is the currently-visible session list in display order
 * (filtered + sorted + collapse-flattened — i.e. exactly what the user sees).
 *
 *   - the row directly below the removed one, else
 *   - the row directly above it (it was the last row), else
 *   - null  → it was the only row, or it isn't in the visible list; the caller
 *             falls back to the sessions list (mobile) / welcome pane (desktop).
 */
export function nextVisibleSessionId(
  orderedIds: readonly string[],
  removedId: string,
): string | null {
  const i = orderedIds.indexOf(removedId)
  if (i === -1) return null
  if (i + 1 < orderedIds.length) return orderedIds[i + 1]
  if (i - 1 >= 0) return orderedIds[i - 1]
  return null
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `web/`): `npx vitest run src/session/nextAfterArchive.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/nextAfterArchive.ts web/src/session/nextAfterArchive.test.ts
git commit -m "feat(web): nextVisibleSessionId helper for post-archive advance"
```

---

### Task 2: Desktop wiring (`DesktopShell`)

**Files:**
- Modify: `web/src/components/DesktopShell.tsx`
- Test: `web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx` (create)

**Interfaces:**
- Consumes: `nextVisibleSessionId` from Task 1.
- Produces: desktop behavior — archiving/deleting the **open** session moves `selection` to the neighbor; archiving a **non-selected** sidebar card leaves `selection` unchanged. No new exports.

Background (already in the file): `selection` is `{ type: 'new' } | { type: 'session'; id; initialPrompt? }`; `orderedSessions` is the visible ordered list; `MainPaneSession` gets an `onClosed` callback; `handleClose(id)` and `confirmSessionDelete()` handle the sidebar card archive/delete. All three run synchronously at click time, so `orderedSessions` read inside them is the pre-archive list.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DesktopShell } from '../DesktopShell'
import type { Session } from '@/api/types'

const SESSIONS: Session[] = [
  { id: 'sess-1', name: 'First', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
  { id: 'sess-2', name: 'Second', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
]

// closeSession fires onSuccess synchronously so the host's onDone runs in-test.
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    useAccounts: () => ({ data: [{ name: 'alice', status: 'ok', usage: { session: { pct: 0, resetsAt: null }, weekly: { pct: 0, resetsAt: null } }, liveSessions: { active: 0, idle: 0 }, rank: 1, rankReason: null }] }),
    useSessions: () => ({ data: SESSIONS, isLoading: false }),
    useArchivedSessions: () => ({ data: [] }),
    useGithubRepos: () => ({ data: [], isLoading: false, isError: false }),
    useCreateSession: () => ({ mutate: vi.fn(), isPending: false }),
    useRenameSession: () => ({ mutate: vi.fn(), isPending: false }),
    usePermanentDeleteSession: () => ({ mutate: vi.fn(), isPending: false }),
    useSendPrompt: () => ({ mutate: vi.fn() }),
    useCloseSession: () => ({ mutate: (_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.(), isPending: false }),
  }
})

// Keep the runtime subtree inert; the title comes from context.
vi.mock('../session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useSessionRuntimeContext: () => ({ title: 'Open', commandResult: null, commands: [], dismissCommandResult: vi.fn() }),
}))
vi.mock('../session/SessionBody', () => ({ SessionBody: () => <div /> }))
vi.mock('../session/SessionComposer', () => ({ SessionComposer: () => <div /> }))
vi.mock('../session/CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear() // default 'live' ordering: groupBy none, sortBy status
})

describe('DesktopShell — advance on archive', () => {
  it('advances the detail selection to the neighbor when the open session is archived', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />)

    // Open sess-1 from the sidebar, then archive it from the detail header.
    await user.click(screen.getByRole('button', { name: /first/i }))
    await user.click(screen.getByRole('button', { name: /archive session open/i }))

    // sess-2 (the row that takes its place) is now the selected row.
    expect(document.getElementById('row-sess-2')).toHaveAttribute('data-selected', 'true')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/DesktopAdvanceOnArchive.test.tsx`
Expected: FAIL — after archive the selection resets to the New Session slot, so `row-sess-2` is not `data-selected="true"` (today's behavior goes to the welcome pane).

- [ ] **Step 3: Add the `advanceFrom` helper and import**

Add the import near the other `@/session` imports (around line 56):

```ts
import { nextVisibleSessionId } from '@/session/nextAfterArchive'
```

Add the helper just after `focusSession` (around line 161), so it closes over the current `orderedSessions`:

```ts
// Map a session that just left the live list to the next detail target: the
// neighbor that takes its place, else the New Session slot. orderedSessions is
// read at call time (pre-archive, since callers run synchronously on click).
const advanceFrom = useCallback((removedId: string): Target => {
  const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), removedId)
  return next ? { type: 'session', id: next } : { type: 'new' }
}, [orderedSessions])
```

- [ ] **Step 4: Route the three removal sites through `advanceFrom`**

`handleClose` (around line 211) — advance only when the archived card is the open one:

```ts
const handleClose = useCallback((id: string) => {
  closeSession.mutate(id)
  setSelection((cur) => (cur.type === 'session' && cur.id === id ? advanceFrom(id) : cur))
}, [closeSession, advanceFrom])
```

`confirmSessionDelete` (around line 238) — same substitution inside its `onSuccess`:

```ts
function confirmSessionDelete() {
  if (!sessionDeleteTarget) return
  const id = sessionDeleteTarget.id
  permanentDeleteSession.mutate(id, {
    onSuccess: () => {
      setSessionDeleteTarget(null)
      // If the deleted session was open, advance to its neighbor.
      setSelection((cur) => (cur.type === 'session' && cur.id === id ? advanceFrom(id) : cur))
    },
    onError: () => setSessionDeleteTarget(null),
  })
}
```

`MainPaneSession`'s `onClosed` (the detail-header archive/delete path, around line 437) — change the prop passed in the `mainPane` assignment:

```ts
mainPane = selection.type === 'session'
  ? <MainPaneSession sessionId={selection.id} onClosed={() => setSelection(advanceFrom(selection.id))} />
  : <WelcomePane />
```

- [ ] **Step 5: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/components/__tests__/DesktopAdvanceOnArchive.test.tsx`
Expected: PASS.

- [ ] **Step 6: Typecheck + lint**

Run (from `web/`): `npx tsc -b --force && npx eslint src/components/DesktopShell.tsx src/components/__tests__/DesktopAdvanceOnArchive.test.tsx --max-warnings 0`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx
git commit -m "feat(web): advance desktop detail pane to neighbor on archive/delete"
```

---

### Task 3: Mobile wiring (`SessionView` + `SessionRoute`)

**Files:**
- Modify: `web/src/components/session/SessionView.tsx`
- Modify: `web/src/App.tsx:51-65` (`SessionRoute`)
- Test: `web/src/App.test.tsx` (add cases)

**Interfaces:**
- Consumes: `nextVisibleSessionId` (Task 1); `useSessions`, `useSessionOrdering` (existing).
- Produces: `SessionView` gains an `onDone: () => void` prop, distinct from `onBack`, wired into `SessionActions.onDone`. `SessionRoute` computes the neighbor and navigates to `/session/{next}` or `/sessions`.

- [ ] **Step 1: Write the failing tests**

Add to `web/src/App.test.tsx`. The existing file mocks `./api/client` so `api.get('/sessions')` resolves the mock list — extend that list to two sessions so there's a neighbor (update the existing `MOCK_SESSIONS` const to include both rows), then add:

```tsx
// MOCK_SESSIONS must contain two live sessions, e.g. ids 'sess-1' and 'sess-2'.
// (Update the existing MOCK_SESSIONS const accordingly.)

it('mobile: archiving the open session advances to the neighbor route', async () => {
  const user = userEvent.setup()
  const seen: string[] = []
  function LocationSpy() {
    const loc = useLocation()
    seen.push(loc.pathname)
    return null
  }
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/session/sess-1']}>
        <App />
        <LocationSpy />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() => expect(screen.getByRole('button', { name: /archive session/i })).toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: /archive session/i }))
  await waitFor(() => expect(seen).toContain('/session/sess-2'))
})

it('mobile: the Back button still returns to the list (no advance)', async () => {
  const user = userEvent.setup()
  const seen: string[] = []
  function LocationSpy() {
    const loc = useLocation()
    seen.push(loc.pathname)
    return null
  }
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/session/sess-1']}>
        <App />
        <LocationSpy />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() => expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: /back to session list/i }))
  await waitFor(() => expect(seen).toContain('/sessions'))
})
```

Note: `useCloseSession` will issue a real `DELETE /sessions/sess-1` against the mocked `api`. Ensure the `api.del` mock in `App.test.tsx` resolves (add `del: vi.fn().mockResolvedValue(undefined)` to the `api` mock if not already present) so `onSuccess` fires.

- [ ] **Step 2: Run tests to verify they fail**

Run (from `web/`): `npx vitest run src/App.test.tsx`
Expected: the advance test FAILS (today archive navigates to `/sessions`, never `/session/sess-2`); the Back test passes (unchanged behavior) — that's fine, it's a guard against regression.

- [ ] **Step 3: Add the `onDone` prop to `SessionView`**

In `web/src/components/session/SessionView.tsx`, extend `Props` (around line 35) and wire it to `SessionActions` only — leave `onBack`/`handleBack` (Back button, Esc, ←) alone:

```ts
interface Props {
  sessionId: string
  onBack: () => void
  /** Called after archive/delete — host advances to the neighbor (distinct from onBack). */
  onDone: () => void
  initialPrompt?: string
}

export function SessionView({ sessionId, onBack, onDone, initialPrompt }: Props) {
```

Add a detach-then-advance wrapper next to `handleBack` (around line 52):

```ts
const handleDone = useCallback(() => {
  detach(sessionId)
  onDone()
}, [sessionId, onDone, detach])
```

Change the `SessionActions` usage (around line 138) from `onDone={handleBack}` to:

```tsx
<SessionActions
  sessionId={sessionId}
  sessionName={rt.title}
  onDone={handleDone}
  variant="dark"
  canRename
/>
```

- [ ] **Step 4: Compute the neighbor in `SessionRoute`**

In `web/src/App.tsx`, add imports at the top:

```ts
import { useSessions } from './api/hooks'
import { useSessionOrdering } from './session/useSessionOrdering'
import { nextVisibleSessionId } from './session/nextAfterArchive'
```

Replace `SessionRoute` (lines 51-65) with:

```tsx
function SessionRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const { data: sessions = [] } = useSessions()
  // Persisted sort/group order (no search filter — the search box isn't reachable
  // from inside a full-screen session). Same order the list showed.
  const { orderedSessions } = useSessionOrdering(sessions)
  const initialPrompt = (location.state as { initialPrompt?: string } | null)?.initialPrompt
  if (!sessionId) return <Navigate to="/sessions" replace />
  const handleDone = () => {
    const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), sessionId)
    navigate(next ? `/session/${next}` : '/sessions')
  }
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <SessionView sessionId={sessionId} onBack={() => navigate('/sessions')} onDone={handleDone} initialPrompt={initialPrompt} />
    </div>
  )
}
```

Note on hook order: `useSessions`/`useSessionOrdering` are called before the `if (!sessionId)` early return, so the hook order stays stable across renders (Rules of Hooks).

- [ ] **Step 5: Run tests to verify they pass**

Run (from `web/`): `npx vitest run src/App.test.tsx`
Expected: PASS (advance + Back cases).

- [ ] **Step 6: Typecheck + lint**

Run (from `web/`): `npx tsc -b --force && npx eslint src/App.tsx src/components/session/SessionView.tsx --max-warnings 0`
Expected: no errors. (`tsc` will flag any other `SessionView` caller missing the now-required `onDone` prop — grep `SessionView` to confirm `SessionRoute` is the only one; if another exists, pass `onDone` there too.)

- [ ] **Step 7: Commit**

```bash
git add web/src/App.tsx web/src/components/session/SessionView.tsx web/src/App.test.tsx
git commit -m "feat(web): advance mobile session route to neighbor on archive/delete"
```

---

### Task 4: Full verification + finish

**Files:** none (verification + integration).

- [ ] **Step 1: Run the full web suite**

Run (from `web/`): `npx vitest run && npx tsc -b --force && npx eslint . --max-warnings 0`
Expected: all green. The full vitest run (not a subset) guards against fixture drift from concurrently-merged changes.

- [ ] **Step 2: Confirm `SessionView` has no other callers left untyped**

Run: `grep -rn '<SessionView' web/src`
Expected: only `SessionRoute` in `App.tsx`. If a test renders `SessionView` directly, give it an `onDone={vi.fn()}` prop.

- [ ] **Step 3: Finish the branch per the completion workflow**

Follow `CLAUDE.md`: branch off `origin/master` (`git fetch` then `git checkout -b <branch> origin/master`), push, open a non-draft PR with `gh pr create`, then `gh pr merge <#> --squash --auto`. PR body references the spec and design.

## Self-Review

- **Spec coverage:** helper + 4 fallback cases (Task 1) ✓; desktop detail-header + sidebar archive + sidebar permanent-delete (Task 2) ✓; mobile archive/delete advance + Back-unchanged (Task 3) ✓; "current filtered+sorted view" — desktop uses `orderedSessions` (search-filtered), mobile uses persisted order without search per design ✓; empty-list fallback (`null` → welcome/list) ✓; `SessionActions` untouched ✓.
- **Placeholder scan:** no TBD/TODO; every code step has full code.
- **Type consistency:** `nextVisibleSessionId(readonly string[], string): string | null` used identically in Tasks 2 and 3; `advanceFrom` returns the file-local `Target` type; `SessionView` `onDone: () => void` matches `SessionActions`' existing `onDone` contract.
