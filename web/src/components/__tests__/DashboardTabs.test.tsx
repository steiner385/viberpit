import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account, Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
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
  useGithubRepos: vi.fn(),
  useModels: vi.fn(() => ({ data: [] })),
  useGithubBranches: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useSubmitReauthCode: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSendPrompt: () => ({ mutate: vi.fn() }),
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useAllPrs: vi.fn(() => ({ data: [] })),
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
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'work',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    ...overrides,
  }
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    name: 'My Session',
    accountName: 'work',
    status: 'idle',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  }
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

/** The top-level tab strip (distinct from the Settings tab's inner section strip). */
function topTabs() {
  return within(screen.getByRole('tablist', { name: /dashboard sections/i }))
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

describe('Dashboard — Sessions/Settings tabs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('renders both Sessions and Settings tabs', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('tab', { name: /sessions/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /settings/i })).toBeInTheDocument()
  })

  it('Sessions tab is selected by default', () => {
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    expect(sessionsTab).toHaveAttribute('aria-selected', 'true')

    const settingsTab = screen.getByRole('tab', { name: /settings/i })
    expect(settingsTab).toHaveAttribute('aria-selected', 'false')
  })

  it('Sessions panel is visible by default', () => {
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [makeSession()],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Sessions panel should be visible (has the session list label)
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
    // The sessions list region should be visible
    expect(screen.queryByRole('region', { name: /account status/i })).not.toBeInTheDocument()
  })

  it('clicking Settings tab then the Accounts section shows the accounts manager', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(topTabs().getByRole('tab', { name: /settings/i }))
    expect(topTabs().getByRole('tab', { name: /settings/i })).toHaveAttribute('aria-selected', 'true')

    // The inner section strip renders; switch to its Accounts section.
    const sections = within(screen.getByRole('tablist', { name: /settings sections/i }))
    await user.click(sections.getByRole('tab', { name: /accounts/i }))

    // The Accounts region content should be visible
    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('clicking Sessions tab after Settings switches back', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [makeSession({ name: 'My Session' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    render(<Dashboard />, { wrapper })

    // Switch to Settings
    await user.click(topTabs().getByRole('tab', { name: /settings/i }))
    // Switch back to Sessions
    await user.click(topTabs().getByRole('tab', { name: /sessions/i }))

    const sessionsTab = topTabs().getByRole('tab', { name: /sessions/i })
    expect(sessionsTab).toHaveAttribute('aria-selected', 'true')
  })

  it('tabs have correct aria-controls pointing to the tabpanel ids', () => {
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    const settingsTab = screen.getByRole('tab', { name: /settings/i })

    const sessionsControlId = sessionsTab.getAttribute('aria-controls')
    const settingsControlId = settingsTab.getAttribute('aria-controls')

    expect(sessionsControlId).toBeTruthy()
    expect(settingsControlId).toBeTruthy()
    expect(sessionsControlId).not.toBe(settingsControlId)

    // The active panel's id should match its tab's aria-controls
    const panel = screen.getByRole('tabpanel')
    expect(panel.id).toBe(sessionsControlId)
  })

  it('arrow-right moves focus from Sessions to Pipeline tab', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    sessionsTab.focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /pipeline/i }))
  })

  it('arrow-left moves focus from Pipeline to Sessions tab', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    const pipelineTab = screen.getByRole('tab', { name: /pipeline/i })
    pipelineTab.focus()
    await user.keyboard('{ArrowLeft}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /sessions/i }))
  })

  it('shows needs-reauth badge on Settings tab when any account needs reauth', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', status: 'needs-reauth' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // The Settings tab should have a visible indicator (a badge element)
    // and the tab's accessible label should mention "needs attention"
    const settingsTab = screen.getByRole('tab', { name: /settings.*needs attention/i })
    expect(settingsTab).toBeInTheDocument()
  })

  it('does NOT show needs-reauth badge when no account needs reauth', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', status: 'ok' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Tab name should NOT include "needs attention"
    expect(screen.queryByRole('tab', { name: /settings.*needs attention/i })).not.toBeInTheDocument()
    // But the Settings tab itself should still exist
    expect(screen.getByRole('tab', { name: /settings/i })).toBeInTheDocument()
  })

  it('shows needs-reauth badge for needs-relogin status too', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'alpha', status: 'needs-relogin', usage: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    const settingsTab = screen.getByRole('tab', { name: /settings.*needs attention/i })
    expect(settingsTab).toBeInTheDocument()
  })

  it('New session button is reachable on the Sessions tab', () => {
    render(<Dashboard />, { wrapper })

    // Should be on Sessions (default) tab — may appear in both header and empty-state
    const newSessionBtns = screen.getAllByRole('button', { name: /new session/i })
    expect(newSessionBtns.length).toBeGreaterThan(0)
  })

  it('groups the session list by status when selected', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [makeSession({ id: 'sess-1', status: 'idle' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.selectOptions(await screen.findByLabelText('Group by'), 'status')
    // idle sessions belong to the 'your-turn' tier, whose header is "Done"
    expect(await screen.findByText('Done')).toBeInTheDocument()
  })
})
