# Master-Detail Sessions Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On wide screens, render the session list in a left sidebar, the focused session in the main pane, Accounts/Archived taking over the main pane, and a single context-aware prompt spanning the bottom — while leaving the existing mobile/full-screen flow untouched.

**Architecture:** Add a `lg`-breakpoint branch in `App.tsx`. Below `lg`, the current `view` state machine renders verbatim. At `lg+`, a new `DesktopShell` composes a sidebar (tabs + session list + "New Session" slot), a main pane, and a bottom prompt. A `useSessionRuntime` hook (extracted from `SessionView`) plus a `SessionRuntimeProvider` context let the main pane and the bottom prompt share one live session. `Composer` and `DispatchBar` keep their existing prop interfaces and are reused as the bottom prompt's two modes.

**Tech Stack:** React 18 + TypeScript, Vite, Tailwind, @tanstack/react-query, xterm/WebSocket (`ws/client.ts` singleton), vitest + @testing-library/react.

## Global Constraints

- Run all web commands from `web/`: `node_modules/.bin/vitest run`, `node_modules/.bin/tsc --noEmit`, `node_modules/.bin/eslint .`. (`node_modules` is symlinked from the main checkout.)
- Desktop layout activates only at Tailwind `lg` (≥1024px). Below `lg`, existing behavior must be byte-for-byte preserved.
- `useMediaQuery` MUST return `false` when `window.matchMedia` is undefined (jsdom) so existing tests stay on the mobile branch.
- Do NOT change the WebSocket protocol or `ws/client.ts`. One focused session is mounted/attached at a time.
- Do NOT change the public prop interfaces of `Composer` or `DispatchBar`.
- Preserve all existing `aria-label`s and roles referenced by tests (e.g. `New session prompt`, `Open session <name>`, `back to session list`, `Message`, `Repository`).
- TDD: failing test → minimal impl → green → commit. Frequent commits.

---

## File Structure

**New files**
- `web/src/lib/useMediaQuery.ts` — `useMediaQuery(query)` hook; jsdom-safe.
- `web/src/lib/__tests__/useMediaQuery.test.ts` — hook tests.
- `web/src/session/useSessionRuntime.ts` — lifts `SessionView`'s socket+reducer+handlers into a reusable hook.
- `web/src/components/session/SessionRuntimeProvider.tsx` — context wrapper around `useSessionRuntime` + `useSessionRuntimeContext()`.
- `web/src/components/session/SessionBody.tsx` — presentational main-pane body (transcript + meta + banners), consumes the runtime.
- `web/src/components/AccountsManager.tsx` — self-contained accounts panel (rows + delete dialog), extracted from `Dashboard`.
- `web/src/components/ArchivedManager.tsx` — self-contained archived panel (list + review + perm-delete dialog), extracted from `Dashboard`.
- `web/src/components/DesktopShell.tsx` — the wide-screen master-detail layout + selection/tab state.
- `web/src/components/__tests__/DesktopShell.test.tsx` — desktop layout tests (stub `matchMedia`).

**Modified files**
- `web/src/components/session/SessionView.tsx` — render via `useSessionRuntime` + `SessionBody` (behavior unchanged).
- `web/src/components/Dashboard.tsx` — use `AccountsManager`/`ArchivedManager` in its accounts/archived panels (output preserved).
- `web/src/App.tsx` — `useMediaQuery` branch: `DesktopShell` at `lg+`, existing layout below.

---

## Task 1: `useMediaQuery` hook

**Files:**
- Create: `web/src/lib/useMediaQuery.ts`
- Test: `web/src/lib/__tests__/useMediaQuery.test.ts`

**Interfaces:**
- Produces: `useMediaQuery(query: string): boolean`

- [ ] **Step 1: Write the failing test**

```ts
// web/src/lib/__tests__/useMediaQuery.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useMediaQuery } from '../useMediaQuery'

afterEach(() => { vi.unstubAllGlobals() })

describe('useMediaQuery', () => {
  it('returns false when matchMedia is unavailable (jsdom default)', () => {
    // jsdom has no matchMedia; ensure it is absent
    vi.stubGlobal('matchMedia', undefined)
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(false)
  })

  it('returns true when matchMedia reports a match', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: true,
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }))
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/lib/__tests__/useMediaQuery.test.ts`
Expected: FAIL — `useMediaQuery` not found.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/lib/useMediaQuery.ts
import { useEffect, useState } from 'react'

/**
 * Subscribe to a CSS media query. Returns false when matchMedia is unavailable
 * (e.g. jsdom in tests) so consumers default to the narrow/mobile branch.
 */
export function useMediaQuery(query: string): boolean {
  const getMatch = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false

  const [matches, setMatches] = useState<boolean>(getMatch)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/lib/__tests__/useMediaQuery.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/useMediaQuery.ts web/src/lib/__tests__/useMediaQuery.test.ts
git commit -m "feat(web): jsdom-safe useMediaQuery hook"
```

---

## Task 2: Extract `useSessionRuntime` hook

Move all socket/reducer/derived/handler logic out of `SessionView` into a hook. `SessionView` becomes a consumer; its rendered output and behavior are unchanged, so the existing `SessionView*` test suites are the safety net.

**Files:**
- Create: `web/src/session/useSessionRuntime.ts`
- Modify: `web/src/components/session/SessionView.tsx`
- Tests (existing, must stay green): `web/src/components/session/__tests__/SessionView.test.tsx`, `SessionView.keyboard.test.tsx`, `SessionView.meta.test.tsx`

**Interfaces:**
- Produces:
```ts
export interface Rotation { from: string; to: string; reason?: string }
export interface SessionRuntime {
  state: import('@/session/reducer').TranscriptState  // whatever initialTranscript's type is
  mode: string
  rotations: Rotation[]
  connectionState: import('@/ws/client').ConnectionState
  title: string
  account: string
  quota: { sessionPct: number; weeklyPct: number } | null
  contextPct: number | null
  subAgents: ReturnType<typeof import('@/session/selectors').selectSubAgents>
  hasOpenAction: boolean
  ariaAnnouncement: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
  onPrompt: (text: string) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  onCancelQueued: (index: number) => void
}
export function useSessionRuntime(sessionId: string): SessionRuntime
```
> Note: if `TranscriptState` is not exported from `@/session/reducer`, export it there (add `export type TranscriptState = ReturnType<typeof transcriptReducer>` or export the existing interface) as part of this task. Use `ReturnType<typeof transcriptReducer>` if no named type exists.

- [ ] **Step 1: Verify the SessionView suite is green first (baseline)**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionView.test.tsx src/components/session/__tests__/SessionView.keyboard.test.tsx src/components/session/__tests__/SessionView.meta.test.tsx`
Expected: PASS. (These are the regression guard for this refactor.)

- [ ] **Step 2: Create the hook by moving logic from `SessionView`**

Create `web/src/session/useSessionRuntime.ts`. Move, verbatim, from `SessionView.tsx`: the `useSessionSocket()` destructure, `useReducer(transcriptReducer, initialTranscript)`, `mode`/`modeRef`/`rotations` state, the attach/detach `useEffect`, the aria-live `useEffect`, `handleDecision`/`handleAnswer`/`handlePrompt`/`handleInterrupt`/`handleSetMode`, the `modeRef` sync effect, `hasOpenAction`, and the derived `title`/`account`/`quota`/`contextPct`/`subAgents`. Return them as `SessionRuntime` (rename handlers to `on*`). Keep imports (`useSessions`, `useAccounts`, `remainingContextPct`, `selectSubAgents`, etc.) in the hook file.

The hook does NOT handle `onBack` or the Esc/Shift+Tab window keydown effect — those stay in `SessionView` (they involve the `onBack` prop and view navigation).

- [ ] **Step 3: Rewrite `SessionView` to consume the hook**

`SessionView` keeps its `Props` (`sessionId`, `onBack`), calls `const rt = useSessionRuntime(sessionId)`, keeps the `handleBack` (`detach` then `onBack`) and the window keydown effect (Esc→back guarded by `rt.hasOpenAction` and composer-with-text; Shift+Tab→`rt.onSetMode(nextPermissionMode(modeRef))`). Render exactly today's markup using `rt.*` (top bar with Back + `rt.title` + `StatusBar`; `SessionMeta`; error banner; `TranscriptView`; working indicator; rotation dividers; action-needed banner; `Composer` fed by `rt.state.status`/`rt.mode`/`rt.onPrompt`/`rt.onInterrupt`/`rt.onSetMode`/`rt.state.queued`/`rt.onCancelQueued`; aria-live region with `rt.ariaAnnouncement`).
> `detach` is needed in `SessionView.handleBack`; export `detach` from the hook return as well, or call `useSessionSocket().detach` directly in `SessionView`. Prefer calling `useSessionSocket()` in `SessionView` only for `detach` to avoid widening the runtime interface.

- [ ] **Step 4: Run the SessionView suite to verify still green**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionView.test.tsx src/components/session/__tests__/SessionView.keyboard.test.tsx src/components/session/__tests__/SessionView.meta.test.tsx`
Expected: PASS (unchanged behavior).

- [ ] **Step 5: Typecheck and commit**

```bash
node_modules/.bin/tsc --noEmit
git add web/src/session/useSessionRuntime.ts web/src/components/session/SessionView.tsx web/src/session/reducer.ts
git commit -m "refactor(web): extract useSessionRuntime from SessionView"
```

---

## Task 3: Extract `SessionBody` presentational component

Split the main-pane content (everything between the top bar and the composer) into a reusable component so both `SessionView` (mobile) and the desktop main pane render identical session content.

**Files:**
- Create: `web/src/components/session/SessionBody.tsx`
- Modify: `web/src/components/session/SessionView.tsx`
- Tests (existing, must stay green): the `SessionView*` suites.

**Interfaces:**
- Consumes: `SessionRuntime` (Task 2)
- Produces: `function SessionBody({ rt }: { rt: SessionRuntime }): JSX.Element` — renders `SessionMeta`, error banner, `TranscriptView`, working indicator, rotation dividers, action-needed banner, and the sr-only aria-live region. Does NOT render the top bar or the composer.

- [ ] **Step 1: Create `SessionBody.tsx`**

Move the JSX for `SessionMeta`, the error banner, `TranscriptView` (props: `turns={rt.state.turns}`, `permissionMode={rt.mode}`, `onDecision={rt.onDecision}`, `onAnswer={rt.onAnswer}`), the working indicator, rotation dividers, action-needed banner, and the aria-live `div` into `SessionBody`, sourced from `rt`.

- [ ] **Step 2: Use `SessionBody` inside `SessionView`**

Replace those JSX blocks in `SessionView` with `<SessionBody rt={rt} />`, keeping the top bar and `<Composer>` as-is.

- [ ] **Step 3: Run the SessionView suite**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/`
Expected: PASS.

- [ ] **Step 4: Typecheck and commit**

```bash
node_modules/.bin/tsc --noEmit
git add web/src/components/session/SessionBody.tsx web/src/components/session/SessionView.tsx
git commit -m "refactor(web): extract SessionBody from SessionView"
```

---

## Task 4: `SessionRuntimeProvider` context

A context that runs `useSessionRuntime` once and shares it with the desktop main pane and the bottom prompt. Keyed remount (by the caller) gives a clean reducer + attach/detach per focused session.

**Files:**
- Create: `web/src/components/session/SessionRuntimeProvider.tsx`
- Test: covered indirectly by Task 7 (`DesktopShell`); add a tiny direct test here.

**Interfaces:**
- Consumes: `useSessionRuntime` (Task 2)
- Produces:
```ts
function SessionRuntimeProvider({ sessionId, children }: { sessionId: string; children: React.ReactNode }): JSX.Element
function useSessionRuntimeContext(): SessionRuntime  // throws if used outside provider
```

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/SessionRuntimeProvider.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionRuntimeProvider, useSessionRuntimeContext } from '../SessionRuntimeProvider'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

function Probe() {
  const rt = useSessionRuntimeContext()
  return <div>title:{rt.title}</div>
}
function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('SessionRuntimeProvider', () => {
  it('provides runtime to descendants (title defaults to sessionId prefix)', () => {
    render(
      <SessionRuntimeProvider sessionId="abcd1234efgh"><Probe /></SessionRuntimeProvider>,
      { wrapper },
    )
    expect(screen.getByText('title:abcd1234')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionRuntimeProvider.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the provider**

```tsx
// web/src/components/session/SessionRuntimeProvider.tsx
import { createContext, useContext } from 'react'
import { useSessionRuntime, type SessionRuntime } from '@/session/useSessionRuntime'

const Ctx = createContext<SessionRuntime | null>(null)

export function SessionRuntimeProvider({
  sessionId, children,
}: { sessionId: string; children: React.ReactNode }) {
  const rt = useSessionRuntime(sessionId)
  return <Ctx.Provider value={rt}>{children}</Ctx.Provider>
}

export function useSessionRuntimeContext(): SessionRuntime {
  const v = useContext(Ctx)
  if (!v) throw new Error('useSessionRuntimeContext must be used within SessionRuntimeProvider')
  return v
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/components/session/__tests__/SessionRuntimeProvider.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/SessionRuntimeProvider.tsx web/src/components/session/__tests__/SessionRuntimeProvider.test.tsx
git commit -m "feat(web): SessionRuntimeProvider context"
```

---

## Task 5: Extract `AccountsManager` and `ArchivedManager`

Make the Accounts and Archived panels self-contained components (own hooks + dialogs) so both `Dashboard` and `DesktopShell` render them. Preserve all aria-labels/roles/text so `DashboardTabs`, `ArchivedTab`, `AccountsNickname`, `AccountDelete`, `AccountReauth`, `SessionActions` tests stay green.

**Files:**
- Create: `web/src/components/AccountsManager.tsx` (move `AccountRow`, `AccountsStrip`, `DeleteAccountDialog`, and the accounts hooks/handlers from `Dashboard`)
- Create: `web/src/components/ArchivedManager.tsx` (move `ArchivedList`, `PermanentDeleteDialog`, and the archived hooks/handlers from `Dashboard`)
- Modify: `web/src/components/Dashboard.tsx` (render `<AccountsManager onReviewSession=... />` and `<ArchivedManager onReviewSession=... />` in the respective panels)
- Tests (existing, must stay green): `DashboardTabs`, `ArchivedTab`, `AccountsNickname`, `AccountDelete`, `AccountReauth`, `SessionActions`.

**Interfaces:**
- Produces:
```ts
function AccountsManager(): JSX.Element            // calls useAccounts/useRefreshAccount/useUpdateAccountNickname/useDeleteAccount/useReauth* internally; renders the "Accounts" heading + rows + delete dialog
function ArchivedManager({ onReviewSession }: { onReviewSession: (id: string) => void }): JSX.Element  // calls useArchivedSessions/useUnarchiveSession/usePermanentDeleteSession; renders heading + list + perm-delete dialog
```

- [ ] **Step 1: Baseline the affected suites**

Run: `node_modules/.bin/vitest run src/components/__tests__/DashboardTabs.test.tsx src/components/__tests__/ArchivedTab.test.tsx src/components/__tests__/AccountsNickname.test.tsx src/components/__tests__/AccountDelete.test.tsx src/components/__tests__/AccountReauth.test.tsx src/components/__tests__/SessionActions.test.tsx`
Expected: PASS.

- [ ] **Step 2: Create `AccountsManager.tsx`**

Move `AccountRow`, `AccountsStrip`, `DeleteAccountDialog` (and their imports) into `AccountsManager.tsx`. In `AccountsManager`, replicate Dashboard's accounts state/handlers: `useAccounts`, `refreshingAccount` state + `handleRefreshAccount`, `useUpdateAccountNickname` + `handleSaveNickname`, `deleteTarget` state + `useDeleteAccount` + `confirmDelete`, and `deleteTargetSessions` (needs `useSessions`). Render the existing `tabpanel-accounts` content: the `Accounts` heading, `<AccountsStrip .../>`, and `{deleteTarget && <DeleteAccountDialog .../>}`. Keep markup/labels identical.

- [ ] **Step 3: Create `ArchivedManager.tsx`**

Move `ArchivedList` and `PermanentDeleteDialog` into `ArchivedManager.tsx`. Replicate Dashboard's archived state/handlers: `useArchivedSessions`, `unarchivingId` + `useUnarchiveSession` + `handleUnarchive`, `permDeleteTarget` + `usePermanentDeleteSession` + `confirmPermanentDelete`, and `handleReview` (call `onReviewSession`). Render the `Archived Sessions` heading + `<ArchivedList .../>` + `{permDeleteTarget && <PermanentDeleteDialog .../>}`. Keep markup/labels identical.

- [ ] **Step 4: Use the new components in `Dashboard`**

In `Dashboard.tsx`, replace the inline accounts panel body with `<AccountsManager />` and the archived panel body with `<ArchivedManager onReviewSession={handleReview} />`. Remove the now-moved functions, state, and handlers from `Dashboard`. Keep the `tabpanel-*` wrapper divs and tab buttons (with the `hasNeedsReauth` dot — `AccountsManager` can export nothing for that; keep `useAccounts` in Dashboard for the dot, or move the dot computation to read `useAccounts` which Dashboard still calls). Keep `role="tabpanel"` wrappers.
> The needs-reauth dot on the Accounts tab uses `accounts` — Dashboard still calls `useAccounts` for the tab list, so leave that call in place.

- [ ] **Step 5: Run the affected suites**

Run: `node_modules/.bin/vitest run src/components/__tests__/DashboardTabs.test.tsx src/components/__tests__/ArchivedTab.test.tsx src/components/__tests__/AccountsNickname.test.tsx src/components/__tests__/AccountDelete.test.tsx src/components/__tests__/AccountReauth.test.tsx src/components/__tests__/SessionActions.test.tsx src/components/__tests__/KeyboardSessions.test.tsx`
Expected: PASS.

- [ ] **Step 6: Typecheck and commit**

```bash
node_modules/.bin/tsc --noEmit
git add web/src/components/AccountsManager.tsx web/src/components/ArchivedManager.tsx web/src/components/Dashboard.tsx
git commit -m "refactor(web): extract AccountsManager and ArchivedManager from Dashboard"
```

---

## Task 6: `DesktopShell` — layout, selection, sidebar, main pane, unified prompt

The wide-screen master-detail composition. Selection state lives here. Reuses `SessionList`, `DispatchBar`, `Composer`, `SessionBody`, `AccountsManager`, `ArchivedManager`, plus the existing `NewSessionDialog`/`RenameDialog`/`ShortcutsHelp`/`SettingsPanel` (import/share from Dashboard — export `NewSessionDialog` and `RenameDialog` from `Dashboard.tsx`, or move them to their own files; prefer exporting to limit churn).

**Files:**
- Create: `web/src/components/DesktopShell.tsx`
- Create: `web/src/components/__tests__/DesktopShell.test.tsx`
- Modify: `web/src/components/Dashboard.tsx` (add `export` to `NewSessionDialog`, `RenameDialog` so `DesktopShell` reuses them; no behavior change)

**Interfaces:**
- Consumes: `SessionList`, `DispatchBar`, `Composer`, `SessionBody`, `useSessionRuntimeContext`, `SessionRuntimeProvider`, `AccountsManager`, `ArchivedManager`, hooks from `@/api/hooks`.
- Produces: `function DesktopShell(): JSX.Element`
- Internal state:
```ts
type TabId = 'sessions' | 'accounts' | 'archived'
type Target = { type: 'new' } | { type: 'session'; id: string }
// selection: Target (default { type: 'new' }); tab: TabId (default 'sessions'); reviewId: string | null
```

**Layout (Tailwind):** a column flex `h-screen`: header (reuse `ccswitch` heading), a `flex-1 flex min-h-0` row with `aside` (`w-80 shrink-0 border-r overflow-y-auto`) + `main` (`flex-1 min-h-0 overflow-hidden flex flex-col`), then the bottom prompt bar (`shrink-0`). The provider wraps the row+prompt so both share runtime.

**Behavior:**
- Sidebar: the three tab buttons (reuse the exact markup/roles from Dashboard) + a list. The list is `SessionList` preceded by a permanent "New Session" row (`aria-label="New session"`, `data-selected` when `selection.type==='new'`). Selecting a real row → `selection = { type:'session', id }` and `tab='sessions'`. Selecting "New Session" → `selection = { type:'new' }`.
- Main pane by `tab`:
  - `accounts` → `<AccountsManager />`
  - `archived` → `reviewId ? <ReviewView sessionId={reviewId} onBack={()=>setReviewId(null)} /> : <ArchivedManager onReviewSession={setReviewId} />`
  - `sessions` → `selection.type==='session' ? <SessionBody rt={useSessionRuntimeContext()} /> : <WelcomeEmptyState />`
- Bottom prompt by `selection` (only meaningful on the `sessions` tab; on accounts/archived tabs render the new-session `DispatchBar` so creating is always available):
  - `selection.type==='new'` → `<DispatchBar ... onDispatch={createAndSelect} onNavigate=... onOpenHighlighted=... onRowAction=... />` (same handlers as Dashboard, but selection drives `DesktopShell` state; on create success set `selection={type:'session',id}`).
  - `selection.type==='session'` → `<Composer .../>` fed from `useSessionRuntimeContext()` (status/mode/queued + on* handlers).
- The provider key: `selection.type==='session' ? selection.id : 'new'`. When `'new'`, render the layout WITHOUT a provider (so `Composer` path is not taken and no attach happens); when a session is selected, wrap in `<SessionRuntimeProvider sessionId={id}>`.
> Implementation detail: split into `<DesktopShellNew/>` and `<DesktopShellSession sessionId/>` subtrees to keep the `useSessionRuntimeContext()` call legal (only mounted under the provider).

- [ ] **Step 1: Export the shared dialogs from Dashboard**

In `Dashboard.tsx`, add `export` to `function NewSessionDialog` and `function RenameDialog`. Run the Dashboard suites to confirm no change:
Run: `node_modules/.bin/vitest run src/components/__tests__/DashboardTabs.test.tsx`
Expected: PASS.

- [ ] **Step 2: Write the failing DesktopShell tests**

```tsx
// web/src/components/__tests__/DesktopShell.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DesktopShell } from '../DesktopShell'
import * as hooks from '@/api/hooks'
import type { Session } from '@/api/types'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(), useSessions: vi.fn(), useCreateSession: vi.fn(),
  useRenameSession: vi.fn(), useCloseSession: vi.fn(), useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(), useDeleteAccount: vi.fn(), useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(), useSettings: vi.fn(), useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(), useReauthStatus: vi.fn(), useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(), useUnarchiveSession: vi.fn(), usePermanentDeleteSession: vi.fn(),
  QK: { accounts: ['accounts'], sessions: ['sessions'], archived: ['archived-sessions'], config: ['config'], githubRepos: ['github-repos'], githubBranches: (r?: string) => ['github-branches', r], settings: ['settings'] },
}))

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }
function mk(o: Partial<Session> = {}): Session {
  return { id: 's1', name: 'One', accountName: 'work', status: 'idle', createdAt: '', updatedAt: '', ...o }
}
function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}
function setup() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [mk()], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  for (const h of ['useCreateSession','useRenameSession','useCloseSession','useRefreshAccount','useUpdateAccountNickname','useDeleteAccount','useUpdateSettings','useReauthAccount','useCancelReauth','useUnarchiveSession','usePermanentDeleteSession'] as const) {
    vi.mocked(hooks[h]).mockReturnValue(NOOP as never)
  }
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
}

describe('DesktopShell', () => {
  beforeEach(() => { vi.clearAllMocks(); setup(); window.localStorage.clear() })

  it('defaults to the New Session target: shows the new-session prompt and a welcome state', () => {
    render(<DesktopShell />, { wrapper })
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/new session/i)).toHaveAttribute('data-selected', 'true')
  })

  it('selecting a session shows its body and switches the prompt to the message composer', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /message/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })

  it('Accounts tab renders the accounts manager in the main pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    expect(screen.getByRole('heading', { name: /^accounts$/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `node_modules/.bin/vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: FAIL — `DesktopShell` not found.

- [ ] **Step 4: Implement `DesktopShell.tsx`**

Implement per the Behavior spec above. Use `SessionRuntimeProvider` keyed by the selected id for the session subtree; render `DispatchBar`/`Composer` accordingly; reuse `NewSessionDialog`/`RenameDialog`/`ShortcutsHelp`/`SettingsPanel`. The "New Session" slot is a `<button aria-label="New session" data-selected={selection.type==='new'} onClick={()=>setSelection({type:'new'})}>`. Wire `DispatchBar.onDispatch` to `createSession.mutate(..., { onSuccess: (s) => setSelection({type:'session', id:s.id}) })`. Keep `aria-label="New session prompt"` (DispatchBar already provides it) and `aria-label="Message"` (Composer already provides it).

- [ ] **Step 5: Run tests to verify they pass**

Run: `node_modules/.bin/vitest run src/components/__tests__/DesktopShell.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 6: Typecheck and commit**

```bash
node_modules/.bin/tsc --noEmit
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/DesktopShell.test.tsx web/src/components/Dashboard.tsx
git commit -m "feat(web): DesktopShell master-detail layout"
```

---

## Task 7: Wire `DesktopShell` into `App` behind the `lg` breakpoint

**Files:**
- Modify: `web/src/App.tsx`
- Test: extend `web/src/App.test.tsx` with a desktop case (stub `matchMedia`).

**Interfaces:**
- Consumes: `useMediaQuery` (Task 1), `DesktopShell` (Task 6).

- [ ] **Step 1: Add the failing desktop test to `App.test.tsx`**

```tsx
// add inside App.test.tsx
it('renders the DesktopShell side-by-side layout at lg and up', async () => {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: true, media: q,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, onchange: null, dispatchEvent: () => false,
  }))
  const { waitFor } = await import('@testing-library/react')
  render(<App />, { wrapper })
  // Desktop: the New Session slot exists in the sidebar (mobile layout has no such slot)
  await waitFor(() => {
    expect(screen.getByLabelText(/^new session$/i)).toBeInTheDocument()
  })
  vi.unstubAllGlobals()
})
```
> Keep the existing 4 mobile tests unchanged — with `matchMedia` undefined they continue to exercise the mobile branch.

- [ ] **Step 2: Run to verify the new test fails, others pass**

Run: `node_modules/.bin/vitest run src/App.test.tsx`
Expected: 4 PASS, 1 FAIL (new desktop test).

- [ ] **Step 3: Implement the branch in `App.tsx`**

```tsx
import { useMediaQuery } from './lib/useMediaQuery'
import { DesktopShell } from './components/DesktopShell'
// ...
function App() {
  const isWide = useMediaQuery('(min-width: 1024px)')
  // existing view-state hooks/handlers stay for the mobile branch
  if (isWide) {
    return (
      <div className="min-h-screen bg-background text-foreground">
        <DesktopShell />
      </div>
    )
  }
  // ...existing mobile return unchanged...
}
```
> Place the `isWide` early-return BEFORE the existing JSX so the mobile path is untouched. All existing `useState`/`useCallback` hooks must remain declared before the conditional return (React hooks rule) — keep them at the top.

- [ ] **Step 4: Run `App.test.tsx`**

Run: `node_modules/.bin/vitest run src/App.test.tsx`
Expected: 5 PASS.

- [ ] **Step 5: Typecheck and commit**

```bash
node_modules/.bin/tsc --noEmit
git add web/src/App.tsx web/src/App.test.tsx
git commit -m "feat(web): render DesktopShell at lg+ breakpoint"
```

---

## Task 8: Full verification + finish

**Files:** none (verification only).

- [ ] **Step 1: Full web test suite**

Run: `node_modules/.bin/vitest run`
Expected: all PASS.

- [ ] **Step 2: Typecheck**

Run: `node_modules/.bin/tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Lint**

Run: `node_modules/.bin/eslint .`
Expected: no errors (fix any introduced).

- [ ] **Step 4: Manual smoke (optional, if a dev environment is available)**

Confirm at a wide viewport: sidebar + session main pane + bottom prompt; New Session slot selected by default; selecting a session swaps the prompt to the composer; Accounts/Archived tabs take over the main pane. Narrow viewport (<1024px): unchanged full-screen flow.

- [ ] **Step 5: Finish the branch**

Per CLAUDE.md completion workflow: push `feat/master-detail-sessions-layout`, open a PR via `gh pr create` (Claude Code trailer), merge with `gh pr merge --merge --delete-branch` once mergeable.

---

## Self-Review

**Spec coverage:**
- Desktop-only at `lg+` → Tasks 1, 7. Mobile untouched → Task 7 early-return + jsdom matchMedia default.
- Unified context-aware bottom prompt → Task 6 (DispatchBar/Composer by selection).
- Selection as single source of truth + "New Session" slot → Task 6.
- Tabs atop sidebar; Accounts/Archived take over main pane → Tasks 5, 6.
- Default selection = New Session → Task 6 (default state + test).
- Single focused session mounted; no ws/client changes → Tasks 2/4/6 (provider keyed by id; one provider mounted).
- Review renders in main pane on desktop → Task 6 (reviewId in archived tab).

**Placeholder scan:** No TBD/TODO; each code step has concrete code or a precise move-this-block instruction with exact symbols.

**Type consistency:** `SessionRuntime` defined in Task 2 and consumed by Tasks 3/4/6 via `useSessionRuntimeContext`. `Target`/`TabId` defined in Task 6. `useMediaQuery` signature consistent across Tasks 1/7. Dialog exports (`NewSessionDialog`, `RenameDialog`) added in Task 6 Step 1 before use.
