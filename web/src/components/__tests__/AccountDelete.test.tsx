import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account, Session } from '@/api/types'
import { anthropicUsage, appSettings } from '@/test/factories'
import { session } from '@/test/factories'
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
  return {
    name: 'work',
    status: 'ok',
    usage: anthropicUsage(30, 50),
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    ...overrides,
  }
}

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

function setupDefaultMocks(deleteMutate = vi.fn()) {
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue({ ...NOOP_MUTATION, mutate: deleteMutate } as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
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
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
  })

  it('renders a Delete button for each account row', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /delete account work/i })).toBeInTheDocument()
  })

  it('clicking Delete opens a confirmation dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    // Switch to Accounts tab

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/permanently removes/i)).toBeInTheDocument()
  })

  it('confirming the delete calls the hook with the account name', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    // Click the destructive Delete button inside the dialog
    const confirmBtn = screen.getByRole('button', { name: /^delete$/i })
    await user.click(confirmBtn)

    expect(deleteMutate).toHaveBeenCalledWith('work', expect.anything())
  })

  it('clicking Cancel in the dialog closes it without calling the hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /delete account/i })).not.toBeInTheDocument()
    })
    expect(deleteMutate).not.toHaveBeenCalled()
  })

  it('pressing Escape on the dialog closes it without calling the hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    await user.keyboard('{Escape}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /delete account/i })).not.toBeInTheDocument()
    })
    expect(deleteMutate).not.toHaveBeenCalled()
  })

  it('shows active-session warning when sessions reference the account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        makeSession({ id: 'sess-1', accountName: 'work', status: 'idle' }),
        makeSession({ id: 'sess-2', accountName: 'work', status: 'in_progress' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    // Should warn about active sessions
    expect(screen.getByText(/2 active session/i)).toBeInTheDocument()
  })

  it('does NOT show active-session warning when no sessions reference the account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))
    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([makeSession({ id: 'sess-1', accountName: 'personal', status: 'idle' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete account work/i }))

    expect(screen.queryByText(/active session/i)).not.toBeInTheDocument()
  })
})
