import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DesktopShell } from '../DesktopShell'
import * as hooks from '@/api/hooks'
import { toast } from 'sonner'
import type { Account, Session, AdoptionCandidate } from '@/api/types'

// Stable socket whose `on` captures handlers, so tests can push session frames
// (e.g. a pending question) through the runtime — same pattern as
// SessionView.keyboard.test.tsx. `capturedHandlers` is reset per test; the
// implementation survives vi.clearAllMocks() (which clears calls, not impls).
let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}
const stableMockSocket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
  on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
    if (!capturedHandlers[event]) capturedHandlers[event] = []
    capturedHandlers[event].push(cb)
  }),
  off: vi.fn(),
  connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => stableMockSocket,
  }
})

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useAllPrs: vi.fn(() => ({ data: [] })),
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
  useAccounts: vi.fn(), useSessions: vi.fn(), useCreateSession: vi.fn(),
  useRenameSession: vi.fn(), useCloseSession: vi.fn(), useRefreshAccount: vi.fn(),
  useUpdateAccount: vi.fn(), useDeleteAccount: vi.fn(), useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(), useSettings: vi.fn(), useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(), useReauthStatus: vi.fn(), useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(), useUnarchiveSession: vi.fn(), usePermanentDeleteSession: vi.fn(),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useModels: vi.fn(), useSendPrompt: vi.fn(), useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(),
  usePendingDeploy: vi.fn(), useDeployStatus: vi.fn(), useStartDeploy: vi.fn(),
  useConfig: vi.fn(), useAdoptable: vi.fn(), useAdoptSession: vi.fn(),
  useAutomations: vi.fn(() => ({ data: [] })),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: { accounts: ['accounts'], sessions: ['sessions'], archived: ['archived-sessions'], config: ['config'], githubRepos: ['github-repos'], githubBranches: (r?: string) => ['github-branches', r], settings: ['settings'] },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }
function mk(o: Partial<Session> = {}): Session {
  return { id: 's1', name: 'One', accountName: 'work', status: 'idle', createdAt: '', updatedAt: '', ...o }
}
function mkAccount(o: Partial<Account> = {}): Account {
  return { name: 'work', status: 'ok', usage: null, liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null, ...o }
}
function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // MemoryRouter: the Settings tab's SettingsView keeps its section in ?section=.
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}
function setup() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [mk()], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useModels).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useModels>)
  for (const h of ['useCreateSession','useRenameSession','useCloseSession','useRefreshAccount','useUpdateAccount','useDeleteAccount','useUpdateSettings','useReauthAccount','useCancelReauth','useUnarchiveSession','usePermanentDeleteSession','useSendPrompt','useFixPr','useStartDeploy'] as const) {
    vi.mocked(hooks[h]).mockReturnValue(NOOP as never)
  }
  // DeployPanel (mounted in DesktopShell) reads these; default to "up to date" so it renders null.
  vi.mocked(hooks.usePendingDeploy).mockReturnValue({ data: undefined } as ReturnType<typeof hooks.usePendingDeploy>)
  vi.mocked(hooks.useDeployStatus).mockReturnValue({ data: undefined } as ReturnType<typeof hooks.useDeployStatus>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  // Adoption is a feature-flagged sidebar panel; default it off so most tests
  // don't render AdoptPanel. Adoption hooks are stubbed empty for the flag-on case.
  vi.mocked(hooks.useConfig).mockReturnValue({ data: { adoptionEnabled: false } } as ReturnType<typeof hooks.useConfig>)
  vi.mocked(hooks.useAdoptable).mockReturnValue({ data: { candidates: [] } } as ReturnType<typeof hooks.useAdoptable>)
  vi.mocked(hooks.useAdoptSession).mockReturnValue(NOOP as never)
}

function renderDesktopShell() {
  return render(<DesktopShell />, { wrapper })
}

describe('DesktopShell', () => {
  beforeEach(() => { vi.clearAllMocks(); capturedHandlers = {}; setup(); window.localStorage.clear() })

  it('defaults to the New Session target: shows the new-session prompt and the slot is selected', () => {
    render(<DesktopShell />, { wrapper })
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/^new session$/i)).toHaveAttribute('data-selected', 'true')
  })

  it('selecting a session shows its body and switches the prompt to the message composer', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })

  it('Settings tab → Accounts section renders the accounts manager in the main pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /settings/i }))
    const sections = within(screen.getByRole('tablist', { name: /settings sections/i }))
    await user.click(sections.getByRole('tab', { name: /accounts/i }))
    expect(screen.getByRole('heading', { name: /^accounts$/i })).toBeInTheDocument()
  })

  it('exposes the Insights and Settings tabs (wide-shell parity with the routed Dashboard)', () => {
    // Regression: these tabs existed only on the narrow routed Dashboard, so on
    // wide screens (which render DesktopShell) they were unreachable.
    render(<DesktopShell />, { wrapper })
    expect(screen.getByRole('tab', { name: /insights/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /settings/i })).toBeInTheDocument()
  })

  it('deleting a session opens a confirm dialog; the mutation runs only on confirm', async () => {
    const user = userEvent.setup()
    const deleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(
      { mutate: deleteMutate, isPending: false, isSuccess: false } as never,
    )
    render(<DesktopShell />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete session one/i }))

    // Destructive: the confirm dialog appears and nothing is deleted yet.
    const dialog = screen.getByRole('dialog')
    expect(deleteMutate).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: /delete permanently/i }))
    expect(deleteMutate).toHaveBeenCalledWith('s1', expect.anything())
  })

  it('ArrowLeft on an empty composer detaches to the welcome pane and highlights the origin row', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByText(/select a session from the left/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /new session prompt/i }))
      .toHaveAttribute('aria-activedescendant', 'row-s1')
  })

  it('ArrowDown on the new-session prompt makes the keyboard-highlighted row visibly selected', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.keyboard('{ArrowDown}')
    // Screen-reader path: the textbox points at the highlighted row.
    expect(prompt).toHaveAttribute('aria-activedescendant', 'row-s1')
    // Sighted path: that row must actually render as selected, not just be the
    // ARIA active descendant. This is the regression that made ↑/↓ look dead.
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
  })

  it('Ctrl+Z detaches to the welcome pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    await user.keyboard('{Control>}z{/Control}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
  })

  it('ArrowLeft with composer text and a mid-prompt caret does NOT detach', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    const box = screen.getByRole('textbox', { name: /^message$/i })
    await user.click(box)
    await user.type(box, 'wip') // caret left at the end (position 3)
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })

  it('ArrowLeft detaches when the composer has text but the caret is at the start', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    const box = screen.getByRole('textbox', { name: /^message$/i }) as HTMLTextAreaElement
    await user.click(box)
    await user.type(box, 'wip')
    box.setSelectionRange(0, 0) // caret at the very start of the prompt
    fireEvent.keyDown(box, { key: 'ArrowLeft' })
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
  })

  // Opens session One, then delivers a live question so the pinned question
  // panel renders with its "Write a response" escape hatch. replayComplete
  // first: the reducer starts in replaying mode, and a question arriving during
  // replay is live:false (the panel only shows live questions).
  async function openSessionWithPendingQuestion(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByLabelText(/open session one/i))
    await act(async () => {
      const emit = (frame: Record<string, unknown>) =>
        capturedHandlers['sessionEvent']?.forEach((cb) => cb(frame))
      emit({ type: 'replayComplete' })
      emit({ type: 'turnStart', turnId: 't1' })
      emit({
        type: 'question', questionId: 'q1', turnId: 't1',
        header: 'Pick one', question: 'Which approach?',
        options: [{ label: 'Option A' }, { label: 'Option B' }], multiSelect: false,
      })
    })
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    return screen.getByRole('textbox', { name: /write a response/i }) as HTMLTextAreaElement
  }

  it('ArrowLeft while typing a custom question answer does NOT detach (draft preserved)', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const box = await openSessionWithPendingQuestion(user)
    await user.type(box, 'my custom answer')
    await user.keyboard('{ArrowLeft}')
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
    expect(box).toHaveValue('my custom answer')
  })

  it('ArrowLeft at the START of a custom question answer still does NOT detach', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const box = await openSessionWithPendingQuestion(user)
    await user.type(box, 'my custom answer')
    box.setSelectionRange(0, 0) // unlike the composer, a non-empty draft never detaches
    fireEvent.keyDown(box, { key: 'ArrowLeft' })
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
    expect(box).toHaveValue('my custom answer')
  })

  it('Escape while typing a custom question answer does NOT detach (draft preserved)', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const box = await openSessionWithPendingQuestion(user)
    await user.type(box, 'my custom answer')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
    expect(box).toHaveValue('my custom answer')
  })

  it('ArrowLeft in an EMPTY custom-answer box detaches (matches the composer left-edge rule)', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const box = await openSessionWithPendingQuestion(user)
    box.focus()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
  })

  it('Tab then a archives the highlighted session', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ ...NOOP, mutate } as never)
    const user = userEvent.setup()
    renderDesktopShell()
    await user.keyboard('{ArrowDown}{Tab}a')
    expect(mutate).toHaveBeenCalled()
  })

  it('DesktopShell: groups by status and keeps keyboard nav', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        mk({ id: 's1', name: 'One', status: 'idle' }),
        mk({ id: 's2', name: 'Two', status: 'in_progress' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    render(<DesktopShell />, { wrapper })
    await user.selectOptions(screen.getByLabelText('Group by'), 'status')
    // Two sessions from DIFFERENT tiers → both group headers must render
    // (idle → 'your-turn' tier, header "Done"; in_progress → 'busy' tier, header "Working")
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Working')).toBeInTheDocument()
    // keyboard nav still works: ArrowDown should highlight something (header or row).
    // With grouped nav, the first ArrowDown lands on the first group header.
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.keyboard('{ArrowDown}')
    expect(prompt.getAttribute('aria-activedescendant')).toMatch(/^(row-s|group-header-)/)
  })

  it('keeps queued sessions out of the grouped list — only QueuedSection shows them', async () => {
    // Regression: queued sessions were fed into useSessionOrdering, and since
    // 'queued' has no status-tier mapping it fell through to the your-turn/"Done"
    // bucket — so a queued session rendered as a full clickable card under "Done"
    // AND in the QueuedSection above. Filtering queued out *before* ordering fixes
    // it (SessionList renders `groups` directly and ignores the flat prop).
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        mk({ id: 'q1', name: 'Queued One', status: 'queued', queuePosition: 1 }),
        mk({ id: 's2', name: 'Live Two', status: 'idle' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    render(<DesktopShell />, { wrapper })
    await user.selectOptions(screen.getByLabelText('Group by'), 'status')

    // The queued session appears only in the QueuedSection…
    expect(screen.getByText('Queued (1)')).toBeInTheDocument()
    // …never as a clickable SessionCard in the grouped "Done" list.
    expect(screen.queryByLabelText(/open session queued one/i)).not.toBeInTheDocument()
    // The live idle session still renders under its "Done" group as before.
    expect(screen.getByLabelText(/open session live two/i)).toBeInTheDocument()
    expect(screen.getByText('Done')).toBeInTheDocument()
  })

  it('shows a subtle toast when a session is created optimistically', async () => {
    const user = userEvent.setup()
    const session = mk({ usageUnverified: true })
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as never)
    render(<DesktopShell />, { wrapper })
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.type(prompt, 'hello')
    await user.keyboard('{Enter}')
    act(() => { capturedCallbacks.onSuccess?.(session) })
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.stringContaining('started anyway'),
    )
  })

  it('routes accounts_need_reauth 409 to a toast, not the limit banner', async () => {
    const user = userEvent.setup()
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as never)
    render(<DesktopShell />, { wrapper })
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.type(prompt, 'hello')
    await user.keyboard('{Enter}')
    act(() => {
      capturedCallbacks.onError?.({ status: 409, body: { error: { code: 'accounts_need_reauth' } } })
    })
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      expect.stringContaining('re-authenticate'),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('auto-clears the all-limited banner once an account becomes selectable', async () => {
    const user = userEvent.setup()
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as never)
    const { rerender } = render(<DesktopShell />, { wrapper })
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.type(prompt, 'hello')
    await user.keyboard('{Enter}')
    act(() => {
      capturedCallbacks.onError?.({ status: 409, body: { error: { code: 'all_accounts_limited' }, earliestReset: null } })
    })
    // Banner should now be visible
    expect(screen.getByRole('alert')).toBeInTheDocument()
    // Now simulate an account recovering (rank becomes non-null)
    vi.mocked(hooks.useAccounts).mockReturnValue({ data: [mkAccount({ rank: 1 })], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
    rerender(<DesktopShell />)
    // Auto-clear effect should remove the banner
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // Regression for #44: the desktop sidebar must surface untracked-session
  // adoption (AdoptPanel), matching the mobile Dashboard's Sessions tab.
  it('renders the AdoptPanel in the sidebar when adoption is enabled', () => {
    const candidate: AdoptionCandidate = {
      pid: 5, account: 'work', cwd: '/home/u/repo', claudeSessionId: 'aaa',
      name: 'Untracked Work', mapped: true, tracked: false, skipReason: null,
      lastActivity: new Date().toISOString(), pidAlive: true,
    }
    vi.mocked(hooks.useConfig).mockReturnValue({ data: { adoptionEnabled: true } } as ReturnType<typeof hooks.useConfig>)
    vi.mocked(hooks.useAdoptable).mockReturnValue({ data: { candidates: [candidate] } } as ReturnType<typeof hooks.useAdoptable>)
    renderDesktopShell()
    expect(screen.getByRole('heading', { name: /untracked sessions/i })).toBeInTheDocument()
    expect(screen.getByText('Untracked Work')).toBeInTheDocument()
  })

  it('omits the AdoptPanel when adoption is disabled', () => {
    renderDesktopShell()
    expect(screen.queryByRole('heading', { name: /untracked sessions/i })).not.toBeInTheDocument()
  })
})
