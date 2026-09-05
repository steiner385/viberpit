/**
 * KeyboardSessions.test.tsx
 *
 * Integration tests for Tasks 3–6: selection state, row actions, help overlay
 * and dispatch — now wired into `AppShell` rather than `Dashboard` (Task 18).
 *
 * The subject did not change: it is still "the prompt bar and the session list
 * share one cursor, and the row keys act on it". The two halves are `PromptDock`
 * and `ContextList` now, both driven by the shell's single `useSessionListState`
 * handle, so the test mounts the real shell at `/sessions` through
 * `test/renderApp` instead of a component that owned both.
 *
 * What that changes in the assertions:
 *   • `onOpenSession` was a prop; opening a session is a NAVIGATION now, so the
 *     assertions read the router's path.
 *   • The "Sessions tab" that focus was parked on is a nav rail LINK.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp } from '@/test/renderApp'
import * as hooks from '@/api/hooks'
import type { Session } from '@/api/types'
import { appSettings } from '@/test/factories'
import { session } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

// ── Mock @/api/hooks (same scaffold as DashboardTabs.test.tsx) ────────────────

// Controllable mock for useSendPrompt
const sendPromptMutate = vi.fn()

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),  // issue #434 indicator
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccount: vi.fn(),
  useDeleteAccount: vi.fn(),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useGithubRepos: vi.fn(),
  useModels: vi.fn(() => ({ data: [] })),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSendPrompt: vi.fn(),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    archived: ['archived-sessions'],
    config: ['config'],
    githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo],
    settings: ['settings'],
  },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  useResumeInterrupted: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRotateStalled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

const makeSession = (overrides: Partial<Session> = {}): Session =>
  session({
    id: 'sess-1',
    name: 'My Session',
    accountName: 'work',
    status: 'idle',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  })

const THREE: Session[] = [
  makeSession({ id: 's1', name: 'One' }),
  makeSession({ id: 's2', name: 'Two' }),
  makeSession({ id: 's3', name: 'Three' }),
]

/** The shell at /sessions, desktop mode, with three sessions in the list. */
function renderDashboard() {
  vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
  return renderApp({ path: '/sessions', mode: 'desktop' })
}

/** The shell at /sessions with whatever `useSessions` is currently mocked to. */
function renderShell() {
  return renderApp({ path: '/sessions', mode: 'desktop' })
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  vi.mocked(hooks.useSendPrompt).mockReturnValue({ mutate: sendPromptMutate, isPending: false } as unknown as ReturnType<typeof hooks.useSendPrompt>)
}

// ── Task 3: Selection state + nav ─────────────────────────────────────────────

describe('Task 3 — session-list selection + keyboard nav', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('ArrowDown highlights the first session, ArrowDown again the second', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session two/i)).toHaveAttribute('data-selected', 'true')
  })

  it('ArrowUp clamps at index 0 (does not go to -1)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}') // index 0
    await user.keyboard('{ArrowUp}')  // should stay at 0
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
  })

  it('Enter on empty prompt opens the highlighted session', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Enter}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Escape clears the highlight (selectedIndex → -1)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}') // index 0 selected
    await user.keyboard('{Escape}')
    // after escape, no session row should be data-selected=true
    for (const sess of THREE) {
      const btn = screen.getByLabelText(`Open session ${sess.name}`)
      expect(btn).not.toHaveAttribute('data-selected', 'true')
    }
  })

  it('dispatch textarea has aria-activedescendant pointing at the selected row', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.keyboard('{ArrowDown}')
    expect(box).toHaveAttribute('aria-activedescendant', 'row-s1')
  })

  it('nav is a no-op when the session list is empty', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Enter}')
    expect(app.path()).toBe('/sessions')
  })

  it('ArrowDown navigates the list even when focus is on the Sessions rail item (not the prompt)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    // Reach the list the way a keyboard user does: focus somewhere in the shell
    // chrome that is NOT the prompt textarea — the rail's Sessions link, which
    // is what `TabBar`'s Sessions tab became. Up/Down must still move the
    // highlight, because the handler is on the window, not the textarea.
    screen.getByRole('link', { name: /^Sessions/ }).focus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session two/i)).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowUp}')
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
  })

  it('ArrowDown while focused in the Repository select is NOT hijacked by list nav', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    renderShell()
    // Arrow keys inside a form control (the repo <select>) must drive that control,
    // not the session list — otherwise picking a repo by keyboard is impossible.
    const select = screen.getByLabelText('Repository')
    select.focus()
    await user.keyboard('{ArrowDown}')
    for (const sess of THREE) {
      expect(screen.getByLabelText(`Open session ${sess.name}`)).not.toHaveAttribute('data-selected', 'true')
    }
  })
})

// ── Task 4: Row actions ───────────────────────────────────────────────────────

describe('Task 4 — row actions on highlighted session', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('Tab then r opens the rename dialog for the highlighted session', async () => {
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}')
    await user.keyboard('{Tab}')
    await user.keyboard('r')
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })

  it('Tab then a archives the highlighted session (single press)', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useCloseSession>)
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}a')
    expect(mutate).toHaveBeenCalledWith('s1')
  })

  it('Tab then c generates a fresh suggestion via the LLM, then resumes with it', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useSendPrompt).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useSendPrompt>)
    // The sparkle now ALWAYS asks the LLM (never a hard-coded "continue").
    vi.mocked(hooks.useGenerateSuggestions).mockReturnValue({
      ...NOOP_MUTATION, mutateAsync: vi.fn(() => Promise.resolve(['Keep going'])),
    } as unknown as ReturnType<typeof hooks.useGenerateSuggestions>)
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}c')
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ text: 'Keep going' })))
  })

  it('Tab then d opens the permanent-delete dialog', async () => {
    const user = userEvent.setup()
    renderDashboard()
    await user.keyboard('{ArrowDown}{Tab}d')
    expect(screen.getByRole('dialog', { name: /delete/i })).toBeInTheDocument()
  })

  it('Alt+3 opens the third session directly', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}3{/Alt}')
    expect(app.path()).toBe('/sessions/s3')
  })

  it('Alt+9 is a no-op when fewer than 9 sessions', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}9{/Alt}')
    expect(app.path()).toBe('/sessions')
  })

  it('selectedIndex clamps when sessions list shrinks', async () => {
    const user = userEvent.setup()
    // Start with 3 sessions, select the last one (index 2).
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(THREE))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}')
    expect(screen.getByLabelText(/open session three/i)).toHaveAttribute('data-selected', 'true')

    // Now shrink the list to 2. Re-reading a mocked hook needs the shell to
    // re-render, and `rerender` cannot do it: the route element `<AppShell/>` is
    // built once inside `buildRoutes`, so React bails on the identical element.
    // A location change reaches it through context instead, which is how the app
    // itself re-renders on every navigation.
    const TWO = THREE.slice(0, 2)
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult(TWO))
    await act(async () => { await app.router.navigate('/sessions?tick=1') })

    // After clamp, index should be 1 (last item in TWO)
    await waitFor(() => {
      expect(screen.getByLabelText(/open session two/i)).toHaveAttribute('data-selected', 'true')
    })
  })
})

// ── Pending placeholder guard ─────────────────────────────────────────────────

describe('Pending placeholder — keyboard open is a no-op', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('Enter on a highlighted pending session does not call onOpenSession', async () => {
    const user = userEvent.setup()
    const pending = makeSession({ id: 'pending-1', name: 'Starting…', status: 'in_progress', pending: true })
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([pending]))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Enter}')
    expect(app.path()).toBe('/sessions')
  })

  it('Alt+1 jump to a pending session does not call onOpenSession', async () => {
    const user = userEvent.setup()
    const pending = makeSession({ id: 'pending-1', name: 'Starting…', status: 'in_progress', pending: true })
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([pending]))
    const app = renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}1{/Alt}')
    expect(app.path()).toBe('/sessions')
  })
})

// ── Help slide-out (the shell's ?drawer=help) ───────────────────────────────

describe('Task 5 — ? shortcuts help overlay', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('? opens the help dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
  })

  it('Esc closes the help dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    renderShell()
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: /help/i })).not.toBeInTheDocument()
  })
})

// ── Task 6: Dispatch create-and-open + auto-focus ─────────────────────────────

describe('Task 6 — dispatch create-and-open', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('Enter with text creates a session with the prompt + repo default branch and opens it', async () => {
    const user = userEvent.setup()
    const createMutate = vi.fn((_input: unknown, opts: { onSuccess?: (s: { id: string }) => void }) => opts?.onSuccess?.({ id: 'new-1' }))
    vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue({
      data: [{ nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: true }],
      isLoading: false, isError: false,
    } as ReturnType<typeof hooks.useGithubRepos>)
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    const app = renderShell()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'add a button')
    await user.keyboard('{Enter}')
    expect(createMutate).toHaveBeenCalledWith(
      { initialPrompt: 'add a button', repo: 'acme/web', branch: 'main' },
      expect.anything(),
    )
    // The prompt is forwarded so the conversation can show it immediately on
    // open. It rides in the router state, not the URL (sessionNavState).
    expect(app.path()).toBe('/sessions/new-1')
    expect(app.router.state.location.state).toEqual({ initialPrompt: 'add a button' })
  })

  it('dispatch with no repo selected omits repo and branch', async () => {
    const user = userEvent.setup()
    const createMutate = vi.fn((_i: unknown, opts: { onSuccess?: (s: { id: string }) => void }) => opts?.onSuccess?.({ id: 'n2' }))
    vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as unknown as ReturnType<typeof hooks.useCreateSession>)
    const app = renderShell()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'hello')
    await user.keyboard('{Enter}')
    expect(createMutate).toHaveBeenCalledWith(
      { initialPrompt: 'hello', repo: undefined, branch: undefined },
      expect.anything(),
    )
    expect(app.path()).toBe('/sessions/n2')
  })
})
