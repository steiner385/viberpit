import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account, Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(),
  useDeleteAccount: vi.fn(),
  useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
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

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'work',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
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
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
}

describe('Dashboard — Sessions/Accounts tabs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('renders both Sessions and Accounts tabs', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('tab', { name: /sessions/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /accounts/i })).toBeInTheDocument()
  })

  it('Sessions tab is selected by default', () => {
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    expect(sessionsTab).toHaveAttribute('aria-selected', 'true')

    const accountsTab = screen.getByRole('tab', { name: /accounts/i })
    expect(accountsTab).toHaveAttribute('aria-selected', 'false')
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

  it('clicking Accounts tab switches to the Accounts panel', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))

    const accountsTab = screen.getByRole('tab', { name: /accounts/i })
    expect(accountsTab).toHaveAttribute('aria-selected', 'true')

    // The Accounts region content should be visible
    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('clicking Sessions tab after Accounts switches back', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [makeSession({ name: 'My Session' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    render(<Dashboard />, { wrapper })

    // Switch to Accounts
    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    // Switch back to Sessions
    await user.click(screen.getByRole('tab', { name: /sessions/i }))

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    expect(sessionsTab).toHaveAttribute('aria-selected', 'true')
  })

  it('tabs have correct aria-controls pointing to the tabpanel ids', () => {
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    const accountsTab = screen.getByRole('tab', { name: /accounts/i })

    const sessionsControlId = sessionsTab.getAttribute('aria-controls')
    const accountsControlId = accountsTab.getAttribute('aria-controls')

    expect(sessionsControlId).toBeTruthy()
    expect(accountsControlId).toBeTruthy()
    expect(sessionsControlId).not.toBe(accountsControlId)

    // The active panel's id should match its tab's aria-controls
    const panel = screen.getByRole('tabpanel')
    expect(panel.id).toBe(sessionsControlId)
  })

  it('arrow-right moves focus from Sessions to Accounts tab', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    const sessionsTab = screen.getByRole('tab', { name: /sessions/i })
    sessionsTab.focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /accounts/i }))
  })

  it('arrow-left moves focus from Accounts to Sessions tab', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    const accountsTab = screen.getByRole('tab', { name: /accounts/i })
    accountsTab.focus()
    await user.keyboard('{ArrowLeft}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /sessions/i }))
  })

  it('shows needs-reauth badge on Accounts tab when any account needs reauth', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', status: 'needs-reauth' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // The Accounts tab should have a visible indicator (a badge element)
    // and the tab's accessible label should mention "needs attention"
    const accountsTab = screen.getByRole('tab', { name: /accounts.*needs attention/i })
    expect(accountsTab).toBeInTheDocument()
  })

  it('does NOT show needs-reauth badge when no account needs reauth', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', status: 'ok' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Tab name should NOT include "needs attention"
    expect(screen.queryByRole('tab', { name: /accounts.*needs attention/i })).not.toBeInTheDocument()
    // But the Accounts tab itself should still exist
    expect(screen.getByRole('tab', { name: /accounts/i })).toBeInTheDocument()
  })

  it('shows needs-reauth badge for needs-relogin status too', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'alpha', status: 'needs-relogin', usage: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    const accountsTab = screen.getByRole('tab', { name: /accounts.*needs attention/i })
    expect(accountsTab).toBeInTheDocument()
  })

  it('New session button is reachable on the Sessions tab', () => {
    render(<Dashboard />, { wrapper })

    // Should be on Sessions (default) tab — may appear in both header and empty-state
    const newSessionBtns = screen.getAllByRole('button', { name: /new session/i })
    expect(newSessionBtns.length).toBeGreaterThan(0)
  })
})
