import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account, Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useSendPrompt: () => ({ mutate: vi.fn() }),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
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
    usage: {
      session: { pct: 30, resetsAt: null },
      weekly: { pct: 50, resetsAt: null },
    },
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
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupDefaultMocks(deleteMutate = vi.fn()) {
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: deleteMutate } as unknown as ReturnType<typeof hooks.useDeleteAccount>)
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
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

describe('AccountRow — Delete button', () => {
  const deleteMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks(deleteMutate)
    vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  })

  it('renders a Delete button for each account row', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Default tab is Sessions; switch to Accounts
    await user.click(screen.getByRole('tab', { name: /accounts/i }))

    expect(screen.getByRole('button', { name: /delete account work/i })).toBeInTheDocument()
  })

  it('clicking Delete opens a confirmation dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Switch to Accounts tab
    await user.click(screen.getByRole('tab', { name: /accounts/i }))

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/permanently removes/i)).toBeInTheDocument()
  })

  it('confirming the delete calls the hook with the account name', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    // Click the destructive Delete button inside the dialog
    const confirmBtn = screen.getByRole('button', { name: /^delete$/i })
    await user.click(confirmBtn)

    expect(deleteMutate).toHaveBeenCalledWith('work', expect.anything())
  })

  it('clicking Cancel in the dialog closes it without calling the hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /delete account/i })).not.toBeInTheDocument()
    })
    expect(deleteMutate).not.toHaveBeenCalled()
  })

  it('pressing Escape on the dialog closes it without calling the hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    await user.keyboard('{Escape}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /delete account/i })).not.toBeInTheDocument()
    })
    expect(deleteMutate).not.toHaveBeenCalled()
  })

  it('shows active-session warning when sessions reference the account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        makeSession({ id: 'sess-1', accountName: 'work', status: 'idle' }),
        makeSession({ id: 'sess-2', accountName: 'work', status: 'in_progress' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    // Should warn about active sessions
    expect(screen.getByText(/2 active session/i)).toBeInTheDocument()
  })

  it('does NOT show active-session warning when no sessions reference the account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [makeSession({ id: 'sess-1', accountName: 'personal', status: 'idle' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    expect(screen.queryByText(/active session/i)).not.toBeInTheDocument()
  })
})
