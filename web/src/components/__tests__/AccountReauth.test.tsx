import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { SettingsView } from '../settings/SettingsView'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import type { ReauthState } from '@/api/types'
import { appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: ({ vncSession }: { vncSession: string }) =>
    <div data-testid="reauth-vnc">{vncSession}</div>,
}))

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

/** The Accounts section tab is pre-selected by the wrapper's ?section=accounts;
 *  clicking it again is a harmless no-op kept for test readability. */
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
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useGithubBranches).mockReturnValue(queryResult({ default: null, branches: [] }))
  vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({
    data: makeReauthState({ state: 'idle' }),
    isLoading: false,
  } as ReturnType<typeof hooks.useReauthStatus>)
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
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows "Re-authenticate" button for needs-reauth account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-reauth' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows a subtle reauth icon button (not the amber label) for ok account', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

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
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /re-authenticate kindash/i }))

    expect(reauthMutate).toHaveBeenCalledWith('kindash', expect.anything())
  })

  it('shows "on the host screen" fallback when running without a vncSession', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running', vncSession: null }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/on the host screen/i)).toBeInTheDocument()
    expect(screen.queryByTestId('reauth-vnc')).not.toBeInTheDocument()
  })

  it('shows the VNC panel and in-page copy when running with a vncSession', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running', vncSession: '__reauth__' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(await screen.findByTestId('reauth-vnc')).toHaveTextContent('__reauth__')
    expect(screen.getByText(/sign in to claude in the window below/i)).toBeInTheDocument()
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

    render(<SettingsView />, { wrapper })
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

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /cancel reauth/i }))

    expect(cancelMutate).toHaveBeenCalledWith('kindash', expect.anything())

    // Optimistically: the status line should vanish (back to idle locally)
    await waitFor(() => {
      expect(screen.queryByText(/on the host screen/i)).not.toBeInTheDocument()
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
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
      </QueryClientProvider>
    )

    render(<SettingsView />, { wrapper: localWrapper })
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

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/Login failed: timeout/i)).toBeInTheDocument()
  })

  it('shows error message when status is timeout (existing)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'timeout', message: 'Re-authentication timed out after 5 minutes.' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText(/timed out/i)).toBeInTheDocument()
  })
})

describe('AccountRow — Generate long-lived token / awaiting_code flow', () => {
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

  it('"Generate long-lived token" button renders on an ok (healthy) account row', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /generate long-lived token for kindash/i })).toBeInTheDocument()
  })

  it('clicking "Generate long-lived token" calls reauth mutate with setup_token mode', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    await user.click(screen.getByRole('button', { name: /generate long-lived token for kindash/i }))

    expect(reauthMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'kindash', mode: 'setup_token' }),
      expect.anything(),
    )
  })

  it('awaiting_code state renders authUrl anchor, Copy button, and code input', async () => {
    const user = userEvent.setup()
    const authUrl = 'https://claude.com/cai/oauth/authorize?x=1'
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'awaiting_code', authUrl } as Partial<ReauthState>),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

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
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'awaiting_code', authUrl } as Partial<ReauthState>),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    const codeInput = screen.getByRole('textbox', { name: /authorization code for kindash/i })
    await user.type(codeInput, 'my-secret-code')

    await user.click(screen.getByRole('button', { name: /submit code/i }))

    expect(submitCodeMutate).toHaveBeenCalledWith(
      { name: 'kindash', code: 'my-secret-code' },
      expect.anything(),
    )
  })

  it('"Generate long-lived token" button is disabled while awaiting_code', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'awaiting_code', authUrl: 'https://claude.com/cai/oauth/authorize?x=1' } as Partial<ReauthState>),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<SettingsView />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByRole('button', { name: /generate long-lived token for kindash/i })).toBeDisabled()
  })
})
