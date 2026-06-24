import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account, ReauthState } from '@/api/types'

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
  useAddAccount: vi.fn(),
  useAddAccountStatus: vi.fn(),
  useCancelAddAccount: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useSendPrompt: () => ({ mutate: vi.fn() }),
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
  return { name: 'kindash', status: 'ok', usage: null, liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null, ...overrides }
}

function makeReauthState(overrides: Partial<ReauthState> = {}): ReauthState {
  return { state: 'idle', message: null, startedAt: null, ...overrides }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

async function switchToAccountsTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('tab', { name: /accounts/i }))
}

const addMutate = vi.fn()
const cancelAddMutate = vi.fn()

function setupDefaultMocks() {
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
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: makeReauthState({ state: 'idle' }), isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useAddAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: addMutate } as unknown as ReturnType<typeof hooks.useAddAccount>)
  vi.mocked(hooks.useAddAccountStatus).mockReturnValue({ data: makeReauthState({ state: 'idle' }), isLoading: false } as ReturnType<typeof hooks.useAddAccountStatus>)
  vi.mocked(hooks.useCancelAddAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: cancelAddMutate } as unknown as ReturnType<typeof hooks.useCancelAddAccount>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [makeAccount()], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
}

describe('AccountsManager — Add account', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('shows an "Add account" button on the accounts tab', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /add account/i })).toBeInTheDocument()
  })

  it('opens a dialog with a name field when "Add account" is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /add account/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText(/account name/i)).toBeInTheDocument()
  })

  it('submitting the name calls the add mutation with that name', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(addMutate).toHaveBeenCalledWith('work', expect.anything())
  })

  it('disables the submit button when the name is empty', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /add account/i }))

    expect(screen.getByRole('button', { name: /^add$/i })).toBeDisabled()
  })

  it('shows the host-machine sign-in hint while the login is running', async () => {
    const user = userEvent.setup()
    // The status query is only enabled once the add has started; model that so
    // the running state appears after clicking Add, not before.
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => ({
      data: makeReauthState({ state: enabled ? 'running' : 'idle' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useAddAccountStatus>))

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(screen.getByText(/on the host machine/i)).toBeInTheDocument()
  })

  it('clicking Cancel during a running add calls the cancel mutation', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => ({
      data: makeReauthState({ state: enabled ? 'running' : 'idle' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useAddAccountStatus>))

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await user.click(screen.getByRole('button', { name: /cancel add/i }))
    expect(cancelAddMutate).toHaveBeenCalled()
  })

  it('invalidates the accounts query when the add succeeds', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => ({
      data: makeReauthState({ state: enabled ? 'success' : 'idle' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useAddAccountStatus>))

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(queryClient, 'invalidateQueries')
    const localWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )

    render(<Dashboard />, { wrapper: localWrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith({ queryKey: ['accounts'] })
    })
  })

  it('shows an error message when the add fails', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => ({
      data: enabled
        ? makeReauthState({ state: 'error', message: 'login did not produce credentials' })
        : makeReauthState({ state: 'idle' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useAddAccountStatus>))

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(screen.getByText(/login did not produce credentials/i)).toBeInTheDocument()
  })
})
