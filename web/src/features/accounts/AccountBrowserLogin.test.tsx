import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from './AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import type { ReauthState } from '@/api/types'
import { appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),  // issue #434 indicator
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
  useSetFastMode: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  usePresets: vi.fn(() => ({ data: [] })),
  useTemplates: vi.fn(() => ({ data: [] })),
  useRenderTemplate: vi.fn(() => ({ mutateAsync: vi.fn() })),
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

const { browserLoginMutate } = vi.hoisted(() => ({ browserLoginMutate: vi.fn() }))
vi.mock('./useBrowserLogin', () => ({
  useBrowserLoginAccount: () => ({ mutate: browserLoginMutate, isPending: false }),
}))
vi.mock('./ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: ({ vncSession }: { vncSession: string }) => (
    <div data-testid="reauth-browser-panel">{vncSession}</div>
  ),
}))

const SIGN_IN = /sign in to claude\.ai for kindash \(remote control\)/i

describe('AccountRow — Sign in to claude.ai (Remote Control)', () => {
  const reauthMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    vi.mocked(hooks.useReauthAccount).mockReturnValue({
      ...NOOP_MUTATION, mutate: reauthMutate,
    } as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue(
      NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  })

  it.each(['login_needed', 'missing'] as const)('is offered when the login is %s', (rc) => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: rc })]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByRole('button', { name: SIGN_IN })).toHaveTextContent(
      'Sign in to claude.ai (Remote Control)')
  })

  it.each(['ok', null, undefined] as const)('is not offered when the login is %s', (rc) => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: rc })]))
    render(<AccountsManager />, { wrapper })
    expect(screen.queryByRole('button', { name: SIGN_IN })).not.toBeInTheDocument()
  })

  it('starts the claude.ai sign-in, not the setup-token reauth', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: 'login_needed' })]))
    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: SIGN_IN }))
    expect(browserLoginMutate).toHaveBeenCalledWith('kindash', expect.anything())
    expect(reauthMutate).not.toHaveBeenCalled()
  })

  it('shows the authorize link and code box, never the in-page login browser', async () => {
    const user = userEvent.setup()
    const submitMutate = vi.fn()
    vi.mocked(hooks.useSubmitReauthCode).mockReturnValue({
      ...NOOP_MUTATION, mutate: submitMutate,
    } as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: 'login_needed' })]))
    render(<AccountsManager />, { wrapper })
    const authUrl = 'https://claude.com/cai/oauth/authorize?code=true&scope=user%3Asessions%3Aclaude_code'
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({
      state: 'awaiting_code', account: 'kindash', authUrl, vncSession: 'reauth' })))
    await user.click(screen.getByRole('button', { name: SIGN_IN }))
    await waitFor(() =>
      expect(screen.getByRole('link', { name: authUrl })).toHaveAttribute('href', authUrl))
    expect(screen.getByText(/open this link in your browser, where you're signed in to claude\.ai/i))
      .toBeInTheDocument()
    expect(screen.queryByTestId('reauth-browser-panel')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: SIGN_IN })).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Authorization code for kindash'), 'CODE#STATE')
    await user.click(screen.getByRole('button', { name: 'Submit code' }))
    expect(submitMutate).toHaveBeenCalledWith(
      { name: 'kindash', code: 'CODE#STATE' }, expect.anything())
  })

  it('says the pasted code is being verified while the sign-in completes', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: 'login_needed' })]))
    render(<AccountsManager />, { wrapper })
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({
      state: 'running', account: 'kindash', vncSession: 'reauth' })))
    await user.click(screen.getByRole('button', { name: SIGN_IN }))
    await waitFor(() => expect(screen.getByText('Verifying the pasted code…')).toBeInTheDocument())
    expect(screen.queryByTestId('reauth-browser-panel')).not.toBeInTheDocument()
  })

  it('says who signed in when the login was for another claude.ai account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'ok', provider: 'anthropic', remoteControlLogin: 'login_needed' })]))
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({
      state: 'error', account: 'kindash', code: 'browser_login_wrong_account',
      message: 'You signed in as b@example.com, expected a@example.com. Nothing was saved.' })))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByRole('alert')).toHaveTextContent(
      'You signed in as b@example.com, expected a@example.com')
  })

  it('never shows the login browser for a setup-token reauth', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ status: 'needs-reauth', provider: 'anthropic', remoteControlLogin: 'ok' })]))
    render(<AccountsManager />, { wrapper })
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState({
      state: 'running', account: 'kindash', vncSession: 'reauth' })))
    await user.click(screen.getByRole('button', { name: /re-authenticate kindash/i }))
    await waitFor(() => expect(screen.getByText('Verifying the pasted code…')).toBeInTheDocument())
    expect(screen.queryByTestId('reauth-browser-panel')).not.toBeInTheDocument()
  })
})
