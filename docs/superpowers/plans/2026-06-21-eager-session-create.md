# Eager Session Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On creating a session via the UI, the card appears instantly, the conversation auto-opens, the thread runs immediately, and the user's prompt shows right away — instead of waiting for the first agent response.

**Architecture:** Frontend-only. We keep the backend's deliberate "fire the pending prompt on WebSocket attach" mechanism; auto-opening the new session makes that attach happen instantly. The user's prompt is rendered optimistically in the transcript reducer and reconciled (re-keyed in place) when the SDK echoes it back as a `user` event.

**Tech Stack:** React + TypeScript + Vite, TanStack Query (react-query), Vitest + Testing Library.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-21-eager-session-create-design.md`.
- No backend changes. Do not remove or alter the `_pending_initial_prompt` / defer-on-attach mechanism.
- Run web checks from the `web/` directory: `vitest run`, `npx tsc --noEmit`, `npx eslint`.
- A fresh worktree has no built env. Symlink `web/node_modules` from the main checkout if absent (see CLAUDE.md "Dev / testing").
- Follow existing patterns: local reducer actions are named with a leading underscore (`_resolvePermission`); tests use the existing mock scaffolds shown in each task.
- The optimistic seed turn uses the sentinel id `optimistic:user` (one ever exists per SessionView mount).

---

### Task 1: Reducer — optimistic seed action + adoption rule

**Files:**
- Modify: `web/src/session/reducer.ts`
- Test: `web/src/session/reducer.optimistic.test.ts` (create)

**Interfaces:**
- Consumes: existing `TranscriptState`, `Turn`, `Block`, `LocalAction`, `transcriptReducer`, `initialTranscript`, helpers `findTurnIdx`, `replaceTurn` from `web/src/session/reducer.ts`.
- Produces:
  - New exported constant `OPTIMISTIC_USER_ID = 'optimistic:user'`.
  - `LocalAction` union gains `{ type: '_seedUserPrompt'; text: string }`.
  - `transcriptReducer` handles `_seedUserPrompt` (appends an optimistic user turn, idempotent) and the existing `user` case adopts the optimistic turn in place (re-keys to the real server `turnId`, no duplicate).

- [ ] **Step 1: Write the failing test**

Create `web/src/session/reducer.optimistic.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript, OPTIMISTIC_USER_ID } from './reducer'

describe('transcriptReducer — optimistic user prompt', () => {
  it('_seedUserPrompt appends one optimistic user turn', () => {
    const s = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'hello world' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0]).toMatchObject({ id: OPTIMISTIC_USER_ID, role: 'user' })
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hello world' })
  })

  it('_seedUserPrompt is idempotent (no duplicate on double-dispatch)', () => {
    const s1 = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'hi' })
    const s2 = transcriptReducer(s1, { type: '_seedUserPrompt', text: 'hi' })
    expect(s2.turns).toHaveLength(1)
    expect(s2).toBe(s1) // unchanged reference
  })

  it('a matching user echo adopts the optimistic turn in place (no duplicate)', () => {
    const seeded = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'do the thing' })
    const adopted = transcriptReducer(seeded, { type: 'user', turnId: 'real-1', text: 'do the thing' })
    expect(adopted.turns).toHaveLength(1)
    expect(adopted.turns[0].id).toBe('real-1')
    expect(adopted.turns[0].blocks[0]).toMatchObject({ id: 'real-1:u', text: 'do the thing' })
  })

  it('a user echo with different text still adopts the single optimistic turn (fallback)', () => {
    const seeded = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'typed text' })
    const adopted = transcriptReducer(seeded, { type: 'user', turnId: 'real-2', text: 'normalized text' })
    expect(adopted.turns).toHaveLength(1)
    expect(adopted.turns[0].id).toBe('real-2')
    expect(adopted.turns[0].blocks[0]).toMatchObject({ text: 'normalized text' })
  })

  it('without a seed, a user event appends normally (unchanged behavior)', () => {
    const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 't1', text: 'plain' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].id).toBe('t1')
  })

  it('a second user event after adoption appends a new turn', () => {
    let s = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'first' })
    s = transcriptReducer(s, { type: 'user', turnId: 'real-1', text: 'first' })
    s = transcriptReducer(s, { type: 'user', turnId: 'real-2', text: 'second' })
    expect(s.turns.map((t) => t.id)).toEqual(['real-1', 'real-2'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/session/reducer.optimistic.test.ts`
Expected: FAIL — `OPTIMISTIC_USER_ID` is not exported / `_seedUserPrompt` not handled.

- [ ] **Step 3: Add the constant and extend the LocalAction union**

In `web/src/session/reducer.ts`, add the constant near the top (after the imports, before `Block`):

```ts
/** Sentinel id for the optimistic user turn seeded before the SDK echoes the prompt. */
export const OPTIMISTIC_USER_ID = 'optimistic:user'
```

Extend `LocalAction`:

```ts
export type LocalAction =
  | { type: '_resolvePermission'; permId: string }
  | { type: '_resolveQuestion';   questionId: string }
  | { type: '_seedUserPrompt';    text: string }
```

- [ ] **Step 4: Add adoption to the `user` case**

In `transcriptReducer`, replace the `case 'user'` block with:

```ts
    case 'user': {
      const { turnId, text } = ev
      const userBlock: Block = { kind: 'text', id: `${turnId}:u`, text }

      // Adopt an optimistic seed turn if present: re-key it in place to the real
      // server turnId instead of appending a duplicate. Only one ever exists, so
      // the first real user event after a seed is unambiguously that prompt.
      const optIdx = findTurnIdx(state.turns, OPTIMISTIC_USER_ID)
      if (optIdx !== -1) {
        const nextTurns = replaceTurn(state.turns, optIdx, () => ({
          id: turnId,
          role: 'user' as const,
          blocks: [userBlock],
        }))
        return { ...state, turns: nextTurns, openBlockId: null }
      }

      const idx = findTurnIdx(state.turns, turnId)
      let nextTurns: Turn[]
      if (idx === -1) {
        // Append new user turn
        nextTurns = [...state.turns, { id: turnId, role: 'user', blocks: [userBlock] }]
      } else {
        // Upsert: replace the text block in the existing turn
        nextTurns = replaceTurn(state.turns, idx, (t) => ({
          ...t,
          blocks: t.blocks.map((b) => (b.kind === 'text' && b.id === `${turnId}:u` ? userBlock : b)),
        }))
      }
      return { ...state, turns: nextTurns, openBlockId: null }
    }
```

- [ ] **Step 5: Add the `_seedUserPrompt` case**

Add this case alongside the other local synthetic actions (after `_resolveQuestion`):

```ts
    case '_seedUserPrompt': {
      // Idempotent: never seed twice (guards React StrictMode double-invoke).
      if (findTurnIdx(state.turns, OPTIMISTIC_USER_ID) !== -1) return state
      const block: Block = { kind: 'text', id: `${OPTIMISTIC_USER_ID}:u`, text: ev.text }
      return {
        ...state,
        turns: [...state.turns, { id: OPTIMISTIC_USER_ID, role: 'user', blocks: [block] }],
        openBlockId: null,
      }
    }
```

- [ ] **Step 6: Run tests to verify they pass**

Run (from `web/`): `npx vitest run src/session/reducer.optimistic.test.ts src/session/reducer.test.ts src/session/reducer.meta.test.ts`
Expected: PASS (new file green; existing reducer tests still green).

- [ ] **Step 7: Typecheck**

Run (from `web/`): `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/reducer.optimistic.test.ts
git commit -m "feat(web): optimistic user-prompt seed + adoption in transcript reducer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Thread `initialPrompt` through App → SessionView and seed on mount

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/session/SessionView.tsx`
- Test: `web/src/components/session/__tests__/SessionView.test.tsx`

**Interfaces:**
- Consumes: `_seedUserPrompt` action and `OPTIMISTIC_USER_ID` from Task 1.
- Produces:
  - `App`'s `View` `session` variant gains `initialPrompt?: string`.
  - `handleOpenSession(sessionId: string, initialPrompt?: string)` — second arg threads the prompt.
  - `Dashboard`'s `onOpenSession` prop type becomes `(sessionId: string, initialPrompt?: string) => void` (consumed by Task 3).
  - `SessionView` Props gain `initialPrompt?: string`; on mount it dispatches `_seedUserPrompt` once when `initialPrompt` is non-empty, before `attach`.

- [ ] **Step 1: Write the failing test**

Add this test to `web/src/components/session/__tests__/SessionView.test.tsx` (reuse the file's existing `stableMockSocket` / `emitSessionEvent` scaffold and its `renderWithClient`/wrapper helper — match whatever the file already uses to render `<SessionView>`):

```ts
describe('SessionView — optimistic initial prompt', () => {
  beforeEach(() => {
    capturedHandlers = {}
    vi.clearAllMocks()
  })

  it('renders the initial prompt immediately as a user message', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SessionView sessionId="s1" onBack={() => {}} initialPrompt="fix the bug" />
      </QueryClientProvider>,
    )
    expect(screen.getByText('fix the bug')).toBeInTheDocument()
  })

  it('does not duplicate the prompt when the user echo arrives', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SessionView sessionId="s1" onBack={() => {}} initialPrompt="fix the bug" />
      </QueryClientProvider>,
    )
    emitSessionEvent({ type: 'user', turnId: 'real-1', text: 'fix the bug' })
    expect(screen.getAllByText('fix the bug')).toHaveLength(1)
  })
})
```

> Note: if the file's render helper differs (e.g. a `renderView()` wrapper), call it with the extra `initialPrompt` prop instead of the inline JSX above. Keep the assertions identical.

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/session/__tests__/SessionView.test.tsx -t "optimistic initial prompt"`
Expected: FAIL — `SessionView` does not accept `initialPrompt`; text not rendered.

- [ ] **Step 3: Add the prop and seed-on-mount to SessionView**

In `web/src/components/session/SessionView.tsx`, extend `Props`:

```ts
interface Props {
  sessionId: string
  onBack: () => void
  initialPrompt?: string
}
```

Update the component signature:

```ts
export function SessionView({ sessionId, onBack, initialPrompt }: Props) {
```

In the existing attach/detach effect (the one keyed on `[sessionId]`), dispatch the seed before `attach(sessionId)`:

```ts
    on('sessionEvent', onEvent)
    on('rotated', onRotated)
    on('error', onError)
    if (initialPrompt) dispatch({ type: '_seedUserPrompt', text: initialPrompt })
    attach(sessionId)
```

(The reducer's `_seedUserPrompt` is idempotent, so StrictMode's double-invoke is safe. `initialPrompt` is intentionally not in the dep array — it is a one-shot mount seed; leave the existing `// eslint-disable-next-line react-hooks/exhaustive-deps` in place.)

- [ ] **Step 4: Run the SessionView tests**

Run (from `web/`): `npx vitest run src/components/session/__tests__/SessionView.test.tsx`
Expected: PASS (new cases green, existing cases unaffected).

- [ ] **Step 5: Thread `initialPrompt` through App.tsx**

In `web/src/App.tsx`, update the `View` type:

```ts
type View =
  | { type: 'dashboard' }
  | { type: 'session'; sessionId: string; originElementId?: string; initialPrompt?: string }
  | { type: 'review'; sessionId: string }
```

Update `handleOpenSession`:

```ts
  const handleOpenSession = useCallback((sessionId: string, initialPrompt?: string) => {
    // The session card button has id="row-<sessionId>" so focus can be restored
    setView({ type: 'session', sessionId, originElementId: `row-${sessionId}`, initialPrompt })
  }, [])
```

Pass it to `SessionView`:

```tsx
        <SessionView sessionId={view.sessionId} onBack={handleBack} initialPrompt={view.initialPrompt} />
```

- [ ] **Step 6: Widen the `onOpenSession` prop type in Dashboard**

In `web/src/components/Dashboard.tsx`, find the `onOpenSession` prop declaration (in the Dashboard component's props interface) and widen it:

```ts
  onOpenSession?: (sessionId: string, initialPrompt?: string) => void
```

(Wiring the prompt into the handlers is Task 3 — this step only makes the type accept it so `App.tsx` and `Dashboard.tsx` compile together.)

- [ ] **Step 7: Typecheck + full web test run**

Run (from `web/`): `npx tsc --noEmit && npx vitest run`
Expected: no type errors; all tests pass (including `App.test.tsx` and `KeyboardSessions.test.tsx`).

- [ ] **Step 8: Commit**

```bash
git add web/src/App.tsx web/src/components/session/SessionView.tsx web/src/components/Dashboard.tsx web/src/components/session/__tests__/SessionView.test.tsx
git commit -m "feat(web): thread initialPrompt into SessionView and seed it on mount

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Auto-open both create paths and pass the prompt (Dashboard)

**Files:**
- Modify: `web/src/components/Dashboard.tsx`
- Test: `web/src/components/__tests__/KeyboardSessions.test.tsx` (add a case) or a new `web/src/components/__tests__/SessionCreateAutoOpen.test.tsx`

**Interfaces:**
- Consumes: `onOpenSession(sessionId, initialPrompt?)` (Task 2); `useCreateSession` mutation returning `Session`; `CreateSessionInput` with `initialPrompt?`.
- Produces: both `handleCreateSubmit` (dialog) and `handleDispatch` (DispatchBar) auto-open the new session and pass the submitted prompt.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/SessionCreateAutoOpen.test.tsx`. Mirror the `vi.mock('@/api/hooks', …)` scaffold from `KeyboardSessions.test.tsx` (copy its full mock object so every hook used by Dashboard is stubbed). Drive `useCreateSession` so its `mutate(input, { onSuccess })` invokes `onSuccess` with a `Session`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Session } from '@/api/types'

// (Copy the full vi.mock('@/api/hooks', …) block from KeyboardSessions.test.tsx)

const createdSession: Session = {
  id: 'new-sess', name: 'New Session', accountName: 'acct-a',
  status: 'idle', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
}

function setupHooks(onOpenSession: (id: string, prompt?: string) => void) {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [{ name: 'acct-a' }] } as never)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] } } as never)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null } } as never)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useCreateSession).mockReturnValue({
    mutate: (input: hooks.CreateSessionInput, opts?: { onSuccess?: (s: Session) => void }) => {
      opts?.onSuccess?.(createdSession)
    },
    isPending: false,
  } as never)
  // Stub the remaining mutation hooks as no-op mutate/isPending objects.
  const noopMut = { mutate: vi.fn(), isPending: false } as never
  vi.mocked(hooks.useRenameSession).mockReturnValue(noopMut)
  vi.mocked(hooks.useCloseSession).mockReturnValue(noopMut)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(noopMut)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: undefined } as never)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(noopMut)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(noopMut)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(noopMut)
}

describe('Dashboard — auto-open on create', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens the new session with its prompt after dialog submit', async () => {
    const onOpenSession = vi.fn()
    setupHooks(onOpenSession)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Dashboard onOpenSession={onOpenSession} onReviewSession={() => {}} focusDispatchSignal={0} />
      </QueryClientProvider>,
    )
    // Open the New Session dialog
    await userEvent.click(screen.getByRole('button', { name: /new session/i }))
    // Type the prompt (textarea/input labelled in the dialog)
    const promptField = screen.getByLabelText(/initial prompt/i)
    await userEvent.type(promptField, 'kick things off')
    // Submit
    await userEvent.click(screen.getByRole('button', { name: /create|start/i }))

    expect(onOpenSession).toHaveBeenCalledWith('new-sess', 'kick things off')
  })
})
```

> Adjust the `getByLabelText`/button-name matchers to the actual dialog copy in `Dashboard.tsx` (the New Session dialog field for `initialPrompt` and its submit button). Keep the final `expect(onOpenSession).toHaveBeenCalledWith('new-sess', 'kick things off')` assertion.

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionCreateAutoOpen.test.tsx`
Expected: FAIL — `onOpenSession` is not called by the dialog path (only the dialog closes).

- [ ] **Step 3: Auto-open + pass prompt in `handleCreateSubmit`**

In `web/src/components/Dashboard.tsx`, update `handleCreateSubmit` so the success path opens the session and forwards the prompt:

```ts
  function handleCreateSubmit(input: CreateSessionInput) {
    createSession.mutate(input, {
      onSuccess: (session) => {
        setShowNewDialog(false)
        if (onOpenSession) onOpenSession(session.id, input.initialPrompt)
      },
      onError: (err) => {
        if (err.status === 409) {
          const body = err.body as { error?: { code?: string; message?: string }; earliestReset?: string | null }
          if (body?.error?.code === 'branch_switch_failed') {
            setBranchError(body.error.message ?? 'Could not switch branch')
          } else {
            const reset = body.earliestReset ?? null
            setAllLimitedReset(reset)
            setShowNewDialog(false)
          }
        }
      },
    })
  }
```

(`newButtonRef.current?.focus()` is dropped from the success path because we now navigate away into the session view; leaving it would try to focus a button that is about to unmount.)

- [ ] **Step 4: Pass the prompt on the dispatch path**

Update `handleDispatch`'s success callback to forward the prompt:

```ts
      {
        onSuccess: (session) => {
          if (repo) setLastRepo(repo)
          if (onOpenSession) onOpenSession(session.id, initialPrompt)
        },
      },
```

- [ ] **Step 5: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionCreateAutoOpen.test.tsx`
Expected: PASS.

- [ ] **Step 6: Run the existing Dashboard test suite (regression)**

Run (from `web/`): `npx vitest run src/components/__tests__/KeyboardSessions.test.tsx src/components/__tests__/DispatchBar.test.tsx`
Expected: PASS. (If a test asserted the dialog only closes on create, update it to also expect navigation.)

- [ ] **Step 7: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/__tests__/SessionCreateAutoOpen.test.tsx
git commit -m "feat(web): auto-open new session and forward the prompt on both create paths

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Optimistic session-card insert in `useCreateSession`

**Files:**
- Modify: `web/src/api/hooks.ts`
- Test: `web/src/api/hooks.createSession.test.tsx` (create)

**Interfaces:**
- Consumes: `QK.sessions`, `api.post`, `Session` type.
- Produces: `useCreateSession.onSuccess` writes the returned `Session` into the `QK.sessions` cache immediately (append if absent, dedupe by `id`), then invalidates.

- [ ] **Step 1: Write the failing test**

Create `web/src/api/hooks.createSession.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useCreateSession, QK } from './hooks'
import { api } from './client'
import type { Session } from './types'

vi.mock('./client', () => ({
  api: { post: vi.fn() },
  ApiRequestError: class extends Error {},
}))

const created: Session = {
  id: 'sess-1', name: 'New Session', accountName: 'a',
  status: 'idle', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
}

describe('useCreateSession — optimistic cache insert', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })

  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }

  it('inserts the created session into the sessions cache on success', async () => {
    qc.setQueryData(QK.sessions, [] as Session[])
    vi.mocked(api.post).mockResolvedValue(created)

    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ initialPrompt: 'go' })

    await waitFor(() => {
      const list = qc.getQueryData<Session[]>(QK.sessions)
      expect(list).toContainEqual(created)
    })
  })

  it('does not duplicate when the session id already exists', async () => {
    qc.setQueryData(QK.sessions, [created] as Session[])
    vi.mocked(api.post).mockResolvedValue(created)

    const { result } = renderHook(() => useCreateSession(), { wrapper })
    result.current.mutate({ initialPrompt: 'go' })

    await waitFor(() => {
      const list = qc.getQueryData<Session[]>(QK.sessions)
      expect(list?.filter((s) => s.id === 'sess-1')).toHaveLength(1)
    })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/api/hooks.createSession.test.tsx`
Expected: FAIL — the created session is not inserted into the cache before the (mocked, never-refetched) poll.

- [ ] **Step 3: Implement the optimistic insert**

In `web/src/api/hooks.ts`, update `useCreateSession`:

```ts
export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    onSuccess: (session) => {
      // Show the new card immediately instead of waiting up to 5s for the poll.
      qc.setQueryData<Session[]>(QK.sessions, (prev) => {
        const list = prev ?? []
        if (list.some((s) => s.id === session.id)) return list
        return [...list, session]
      })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/api/hooks.createSession.test.tsx`
Expected: PASS.

- [ ] **Step 5: Full web verification**

Run (from `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: all tests pass, no type errors, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/hooks.createSession.test.tsx
git commit -m "feat(web): optimistically insert the new session card on create

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Manual verification + finish the branch

**Files:** none (verification + integration).

- [ ] **Step 1: Backend regression sanity**

The change is frontend-only, but confirm nothing in the server tests depends on create-time behavior we touched (we didn't change the server — this is a guard):

Run (from repo root, using the main checkout's prebuilt env per CLAUDE.md):
`<main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (unchanged).

- [ ] **Step 2: Manual smoke (use the `run` skill or existing dev workflow)**

Start the app, create a session **with** an initial prompt via the New Session dialog. Verify:
- the card appears immediately (no ~5s delay),
- the view auto-opens,
- your prompt is visible instantly as the first user message,
- when the agent responds, the prompt is **not** duplicated,
- the assistant reply streams in normally.

Then create a session **without** a prompt and confirm it auto-opens with a clean empty transcript (no stray optimistic turn).

- [ ] **Step 3: Finish the branch**

Use the `superpowers:finishing-a-development-branch` skill (or the CLAUDE.md completion workflow): ensure `vitest run`, `tsc --noEmit`, `eslint`, and `pytest` are green, then commit any remainder, push, open a PR with `gh pr create`, and merge once mergeable.

---

## Self-Review

**Spec coverage:**
- Goal 1 (card appears instantly) → Task 4.
- Goal 2 (auto-open both paths) → Task 3 (+ Task 2 threading).
- Goal 3 (thread underway at creation) → falls out of Task 2/3 auto-open → attach (no code; design §3).
- Goal 4 (show prompt immediately) → Task 1 (reducer) + Task 2 (seed on mount).
- Reconciliation rule (seed + replace on echo, text-match + single-unadopted fallback) → Task 1 `user` case adoption (covered by the matching-text and different-text tests).
- Edge cases: no-prompt session → Task 3/5 (no seed); replay no-collision → brand-new session empty replay (design §Edge cases), exercised in Task 5 Step 2; back-then-reopen passes no `initialPrompt` → Task 2 (one-shot mount seed).
- Non-goal (no backend change) → honored; Task 5 Step 1 only *verifies* server is untouched.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. Test matchers in Tasks 3 note where to align with actual dialog copy but keep concrete assertions.

**Type consistency:** `OPTIMISTIC_USER_ID` defined in Task 1 and consumed in Task 2; `onOpenSession(sessionId, initialPrompt?)` defined in Task 2 Step 6 and consumed in Task 3; `_seedUserPrompt` shape (`{ type; text }`) consistent across Tasks 1–2; `Session` shape matches `web/src/api/types.ts`.
