import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import { anthropicUsage, appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
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
  useModels: vi.fn(() => ({ data: [{ id: 'default', label: 'Default' }, { id: 'opus', label: 'Opus' }, { id: 'fable', label: 'Fable' }] })),
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
  useResumeInterrupted: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRotateStalled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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

describe('AccountsStrip — nickname display & edit', () => {
  const nicknameMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: nicknameMutate,
    } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
    vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  })

  it('shows account name when no nickname is set', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: null })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('shows only the nickname when set — the internal slug is never displayed', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: 'My Work Account' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText('My Work Account')).toBeInTheDocument()
    // The internal slug must not leak into the UI once a nickname is chosen.
    expect(screen.queryByText('work')).not.toBeInTheDocument()
  })

  it('shows pencil edit button with aria-label "Rename {name}"', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /rename work/i })).toBeInTheDocument()
  })

  it('clicking the edit button shows an input prefilled with current nickname', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: 'Work' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))

    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    expect(input).toBeInTheDocument()
    expect((input as HTMLInputElement).value).toBe('Work')
  })

  it('saving the nickname calls the mutation with name and trimmed value', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: null })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.type(input, 'Personal')
    await user.click(screen.getByRole('button', { name: /save account settings/i }))

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: 'Personal', weeklyReservePct: null, allowExtraUsage: false })
  })

  it('pressing Enter in the input saves the nickname', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: null })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.type(input, 'Freelance{Enter}')

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: 'Freelance', weeklyReservePct: null, allowExtraUsage: false })
  })

  it('pressing Escape cancels edit without calling mutation', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: null })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.type(input, 'Something{Escape}')

    expect(nicknameMutate).not.toHaveBeenCalled()
    // Edit mode should be gone
    await waitFor(() => {
      expect(screen.queryByRole('textbox', { name: /nickname for work/i })).not.toBeInTheDocument()
    })
  })

  it('saving with empty value passes null to mutation (clears nickname)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', nickname: 'Old Name' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.click(screen.getByRole('button', { name: /save account settings/i }))

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: null, weeklyReservePct: null, allowExtraUsage: false })
  })
})

describe('AccountRow — null usage guard (needs-reauth accounts)', () => {
  // Regression: before the fix, an account with usage:null (e.g. needs-reauth) crashed
  // the whole app with "Cannot read properties of null (reading 'session')".

  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
    vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  })

  it('renders without throwing when account.usage is null (needs-reauth)', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })]))

    // Should not throw — previously crashed with "Cannot read properties of null"
    expect(() => render(<AccountsManager />, { wrapper })).not.toThrow()
  })

  it('does not render any UsageBadge when account.usage is null', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })]))

    render(<AccountsManager />, { wrapper })

    // UsageBadge renders with aria-label "session usage: N%" / "weekly usage: N%".
    // Neither should be present when usage is null.
    expect(screen.queryByLabelText(/session usage:/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/weekly usage:/i)).not.toBeInTheDocument()
  })

  it('still renders the account name when usage is null', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText('kindash')).toBeInTheDocument()
  })
})
