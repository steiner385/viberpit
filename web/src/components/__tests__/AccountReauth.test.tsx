import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import type { ReauthState } from '@/api/types'
import { anthropicUsage, appSettings } from '@/test/factories'
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
    name: 'kindash',
    status: 'needs-relogin',
    usage: null,
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
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
  // MemoryRouter at ?section=accounts: the Accounts panel is a Settings-tab
  // section now, so the wrapper mounts it active for every test.
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

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
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'idle' })))
  vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
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
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows "Re-authenticate" button for needs-reauth account', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-reauth' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows a subtle reauth icon button (not the amber label) for ok account', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'ok', usage: anthropicUsage(20, 50) })]))

    render(<AccountsManager />, { wrapper })

    // ok accounts are re-authenticatable via the quiet icon affordance,
    // reachable by accessible name...
    const reauthButton = screen.getByRole('button', { name: /re-authenticate kindash/i })
    expect(reauthButton).toBeInTheDocument()
    // ...but not via the loud amber "Re-authenticate" text label (that variant
    // is reserved for needs-reauth / needs-relogin accounts).
    expect(reauthButton).not.toHaveTextContent(/re-authenticate/i)
  })

  it('clicking Re-authenticate calls the POST hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /re-authenticate kindash/i }))

    expect(reauthMutate).toHaveBeenCalledWith('kindash', expect.anything())
  })

  it('shows a "verifying" message while running (no browser/VNC involved)', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'running' })))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText(/verifying the pasted code/i)).toBeInTheDocument()
  })

  it('shows Cancel button while running', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'running' })))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /cancel reauth/i })).toBeInTheDocument()
  })

  it('clicking Cancel calls cancel mutation and optimistically returns to idle', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'running' })))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /cancel reauth/i }))

    expect(cancelMutate).toHaveBeenCalledWith('kindash', expect.anything())

    // Optimistically: the status line should vanish (back to idle locally)
    await waitFor(() => {
      expect(screen.queryByText(/verifying the pasted code/i)).not.toBeInTheDocument()
    })
  })

  it('shows success indicator (✓) when status is success and invalidates accounts', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'success' })))

    // Use a stable QueryClient so we can spy on invalidateQueries
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(queryClient, 'invalidateQueries')
    const localWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
      </QueryClientProvider>
    )

    render(<AccountsManager />, { wrapper: localWrapper })

    // Should show a success indicator with checkmark
    expect(screen.getByText(/✓/)).toBeInTheDocument()

    // Should have invalidated the accounts query to refresh account status
    await waitFor(() => {
      expect(spy).toHaveBeenCalledWith({ queryKey: ['accounts'] })
    })
  })

  it('shows error message when status is error', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'error', message: 'Login failed: timeout' })))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText(/Login failed: timeout/i)).toBeInTheDocument()
  })

  it('shows error message when status is timeout (existing)', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'timeout', message: 'Re-authentication timed out after 5 minutes.' })))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText(/timed out/i)).toBeInTheDocument()
  })
})

describe('AccountRow — awaiting_code flow', () => {
  const reauthMutate = vi.fn()
  const submitCodeMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()

    vi.mocked(hooks.useReauthAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: reauthMutate,
    } as unknown as ReturnType<typeof hooks.useReauthAccount>)

    vi.mocked(hooks.useCancelReauth).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useCancelReauth>)

    vi.mocked(hooks.useSubmitReauthCode).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: submitCodeMutate,
    } as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
  })

  it('awaiting_code state renders authUrl anchor, Copy button, and code input', async () => {
    const authUrl = 'https://claude.com/cai/oauth/authorize?x=1'
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'ok', usage: anthropicUsage(20, 50) })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'awaiting_code', authUrl } as Partial<ReauthState>)))

    render(<AccountsManager />, { wrapper })

    // Auth URL anchor should be present with the correct href and safe rel
    const anchor = screen.getByRole('link', { name: authUrl })
    expect(anchor).toHaveAttribute('href', authUrl)
    expect(anchor).toHaveAttribute('rel', 'noopener noreferrer')

    // Copy button should be present
    expect(screen.getByRole('button', { name: /copy/i })).toBeInTheDocument()

    // Code input should be present
    expect(screen.getByRole('textbox', { name: /authorization code for kindash/i })).toBeInTheDocument()
  })

  it('typing a code and clicking Submit calls submitCode mutate with the entered code', async () => {
    const user = userEvent.setup()
    const authUrl = 'https://claude.com/cai/oauth/authorize?x=1'
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'ok', usage: anthropicUsage(20, 50) })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'awaiting_code', authUrl } as Partial<ReauthState>)))

    render(<AccountsManager />, { wrapper })

    const codeInput = screen.getByRole('textbox', { name: /authorization code for kindash/i })
    await user.type(codeInput, 'my-secret-code')

    await user.click(screen.getByRole('button', { name: /submit code/i }))

    expect(submitCodeMutate).toHaveBeenCalledWith(
      { name: 'kindash', code: 'my-secret-code' },
      expect.anything(),
    )
  })

  it('hides the "Re-authenticate" button while awaiting_code (Cancel takes over)', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'ok', usage: anthropicUsage(20, 50) })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({ state: 'awaiting_code', authUrl: 'https://claude.com/cai/oauth/authorize?x=1' } as Partial<ReauthState>)))

    render(<AccountsManager />, { wrapper })

    expect(screen.queryByRole('button', { name: /re-authenticate kindash/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel reauth/i })).toBeInTheDocument()
  })
})
