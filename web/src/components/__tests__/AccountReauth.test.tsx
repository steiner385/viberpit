import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import type { ReauthState } from '@/api/types'

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
    name: 'kindash',
    status: 'needs-relogin',
    usage: null,
    ...overrides,
  }
}

function makeReauthState(overrides: Partial<ReauthState> = {}): ReauthState {
  return {
    state: 'idle',
    message: null,
    startedAt: null,
    ...overrides,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

/** Accounts are now behind a tab — switch to it before asserting account content. */
async function switchToAccountsTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('tab', { name: /accounts/i }))
}

function setupDefaultMocks() {
  vi.mocked(hooks.useSessions).mockReturnValue({
    data: [],
    isLoading: false,
  } as ReturnType<typeof hooks.useSessions>)
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
  vi.mocked(hooks.useReauthStatus).mockReturnValue({
    data: makeReauthState({ state: 'idle' }),
    isLoading: false,
  } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
}

describe('AccountRow — Re-authenticate button', () => {
  const reauthMutate = vi.fn()
  const cancelMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()

    vi.mocked(hooks.useReauthAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: reauthMutate,
    } as unknown as ReturnType<typeof hooks.useReauthAccount>)

    vi.mocked(hooks.useCancelReauth).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: cancelMutate,
    } as unknown as ReturnType<typeof hooks.useCancelReauth>)
  })

  it('shows "Re-authenticate" button for needs-relogin account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows "Re-authenticate" button for needs-reauth account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-reauth' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('does not show "Re-authenticate" button for ok account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.queryByRole('button', { name: /re-authenticate kindash/i })).not.toBeInTheDocument()
  })

  it('clicking Re-authenticate calls the POST hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /re-authenticate kindash/i }))

    expect(reauthMutate).toHaveBeenCalledWith('kindash', expect.anything())
  })

  it('shows "on the host machine" in the status line when running', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/on the host machine/i)).toBeInTheDocument()
  })

  it('shows Cancel button while running', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /cancel reauth/i })).toBeInTheDocument()
  })

  it('clicking Cancel calls cancel mutation and optimistically returns to idle', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /cancel reauth/i }))

    expect(cancelMutate).toHaveBeenCalledWith('kindash', expect.anything())

    // Optimistically: the status line should vanish (back to idle locally)
    await waitFor(() => {
      expect(screen.queryByText(/on the host machine/i)).not.toBeInTheDocument()
    })
  })

  it('shows success indicator (✓) when status is success and invalidates accounts', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'success' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    // Use a stable QueryClient so we can spy on invalidateQueries
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(queryClient, 'invalidateQueries')
    const localWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )

    render(<Dashboard />, { wrapper: localWrapper })
    await switchToAccountsTab(user)

    // Should show a success indicator with checkmark
    expect(screen.getByText(/✓/)).toBeInTheDocument()

    // Should have invalidated the accounts query to refresh account status
    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith({ queryKey: ['accounts'] })
    })
  })

  it('shows error message when status is error', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'error', message: 'Login failed: timeout' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/Login failed: timeout/i)).toBeInTheDocument()
  })

  it('shows error message when status is timeout', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'timeout', message: 'Re-authentication timed out after 5 minutes.' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/timed out/i)).toBeInTheDocument()
  })
})
