import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import { anthropicUsage, appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),
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
    usage: anthropicUsage(30, 50, { extraUsageEnabled: true }),
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    weeklyReservePct: null,
    allowExtraUsage: false,
    ...overrides,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

describe('AccountRow — extra usage credits', () => {
  const updateMutate = vi.fn()

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
      mutate: updateMutate,
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

  function mockAccounts(...accounts: Account[]) {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult(accounts))
  }

  it('edit mode shows a checkbox reflecting the current opt-in', async () => {
    const user = userEvent.setup()
    mockAccounts(makeAccount({ name: 'work', allowExtraUsage: true }))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /rename work/i }))

    const box = screen.getByRole('checkbox', { name: /extra usage credits for work/i })
    expect((box as HTMLInputElement).checked).toBe(true)
  })

  it('opting in sends allowExtraUsage alongside the other settings', async () => {
    const user = userEvent.setup()
    mockAccounts(makeAccount({ name: 'work', nickname: null, allowExtraUsage: false }))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /rename work/i }))
    await user.click(screen.getByRole('checkbox', { name: /extra usage credits for work/i }))
    await user.click(screen.getByRole('button', { name: /save account settings/i }))

    expect(updateMutate).toHaveBeenCalledWith({
      name: 'work', nickname: null, weeklyReservePct: null, allowExtraUsage: true,
    })
  })

  it('opting back out sends false, not an omitted field', async () => {
    const user = userEvent.setup()
    mockAccounts(makeAccount({ name: 'work', nickname: null, allowExtraUsage: true }))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /rename work/i }))
    await user.click(screen.getByRole('checkbox', { name: /extra usage credits for work/i }))
    await user.click(screen.getByRole('button', { name: /save account settings/i }))

    expect(updateMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'work', allowExtraUsage: false }),
    )
  })

  it('cancelling discards the toggle change', async () => {
    const user = userEvent.setup()
    mockAccounts(makeAccount({ name: 'work', allowExtraUsage: false }))

    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /rename work/i }))
    await user.click(screen.getByRole('checkbox', { name: /extra usage credits for work/i }))
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    expect(updateMutate).not.toHaveBeenCalled()
  })

  it('shows an extra-usage chip on opted-in accounts only', async () => {
    mockAccounts(
      makeAccount({ name: 'work', allowExtraUsage: true }),
      makeAccount({ name: 'personal', allowExtraUsage: false }),
    )

    render(<AccountsManager />, { wrapper })

    expect(screen.getAllByText(/extra usage/i)).toHaveLength(1)
  })

  it('warns when opted in but the provider has extra usage disabled', async () => {
    mockAccounts(makeAccount({
      name: 'work',
      allowExtraUsage: true,
      // off upstream — the opt-in cannot take effect
      usage: anthropicUsage(30, 50, { extraUsageEnabled: false }),
    }))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText(/not enabled upstream/i)).toBeInTheDocument()
  })

  it('does not warn when the provider has extra usage enabled', async () => {
    mockAccounts(makeAccount({ name: 'work', allowExtraUsage: true }))

    render(<AccountsManager />, { wrapper })

    expect(screen.queryByText(/not enabled upstream/i)).not.toBeInTheDocument()
  })

  it('does not warn on an account that never opted in', async () => {
    mockAccounts(makeAccount({
      name: 'work',
      allowExtraUsage: false,
      usage: anthropicUsage(30, 50, { extraUsageEnabled: false }),
    }))

    render(<AccountsManager />, { wrapper })

    expect(screen.queryByText(/not enabled upstream/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/extra usage/i)).not.toBeInTheDocument()
  })
})
