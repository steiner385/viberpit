import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account, ReauthState } from '@/api/types'
import { appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
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
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useSubmitReauthCode: vi.fn(),
  useAddAccount: vi.fn(),
  useAddAccountStatus: vi.fn(),
  useCancelAddAccount: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // Task 5.2: the engine-aware launch path (`useEngineOffers`) and the OpenAI
  // device flow. An empty offer list is the pre-Codex answer — no Agent selector.
  useEngineOffers: vi.fn(() => []),
  useAddOpenAIAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIReauthStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useSessionsViewing: vi.fn(),
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
  // MemoryRouter at ?section=accounts: the Accounts panel is a Settings-tab
  // section now, so the wrapper mounts it active for every test.
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const addMutate = vi.fn()
const cancelAddMutate = vi.fn()

function setupDefaultMocks() {
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
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'idle' })))
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
  vi.mocked(hooks.useAddAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: addMutate } as unknown as ReturnType<typeof hooks.useAddAccount>)
  vi.mocked(hooks.useAddAccountStatus).mockReturnValue(queryResult(makeReauthState({ state: 'idle' })))
  vi.mocked(hooks.useCancelAddAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: cancelAddMutate } as unknown as ReturnType<typeof hooks.useCancelAddAccount>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount()]))
}

describe('AccountsManager — Add account', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('shows an "Add account" button on the accounts tab', async () => {
    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /add account/i })).toBeInTheDocument()
  })

  it('opens a dialog with a name field when "Add account" is clicked', async () => {
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /add account/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText(/account name/i)).toBeInTheDocument()
  })

  it('submitting the name calls the add mutation with that name', async () => {
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(addMutate).toHaveBeenCalledWith('work', expect.anything())
  })

  it('disables the submit button when the name is empty', async () => {
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /add account/i }))

    expect(screen.getByRole('button', { name: /^add$/i })).toBeDisabled()
  })

  it('shows the host-machine sign-in hint while the login is running', async () => {
    const user = userEvent.setup()
    // The status query is only enabled once the add has started; model that so
    // the running state appears after clicking Add, not before.
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => (queryResult(makeReauthState({ state: enabled ? 'running' : 'idle' }))))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(screen.getByText(/on the host screen/i)).toBeInTheDocument()
  })

  it('clicking Cancel during a running add calls the cancel mutation', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => (queryResult(makeReauthState({ state: enabled ? 'running' : 'idle' }))))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await user.click(screen.getByRole('button', { name: /cancel add/i }))
    expect(cancelAddMutate).toHaveBeenCalled()
  })

  it('switching provider during a running add calls the cancel mutation', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) =>
      queryResult(makeReauthState({ state: enabled ? 'running' : 'idle' })))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    await user.click(screen.getByRole('button', { name: /openai \(chatgpt\)/i }))

    expect(cancelAddMutate).toHaveBeenCalled()
  })

  it('invalidates the accounts query when the add succeeds', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAddAccountStatus).mockImplementation((enabled: boolean) => (queryResult(makeReauthState({ state: enabled ? 'success' : 'idle' }))))

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(queryClient, 'invalidateQueries')
    const localWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
      </QueryClientProvider>
    )

    render(<AccountsManager />, { wrapper: localWrapper })
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

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /add account/i }))
    await user.type(screen.getByLabelText(/account name/i), 'work')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(screen.getByText(/login did not produce credentials/i)).toBeInTheDocument()
  })
})
