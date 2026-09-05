import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
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

describe('AccountsStrip — session counts & next-pick rank', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useGithubBranches).mockReturnValue(queryResult({ default: null, branches: [] }))
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

  async function renderWith(account: Partial<Account>) {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount(account)]))
    render(<AccountsManager />, { wrapper })
  }

  it('shows split active/idle session counts', async () => {
    await renderWith({ liveSessions: { active: 2, idle: 1 } })
    expect(screen.getByText('2 active · 1 idle')).toBeInTheDocument()
  })

  it('shows a dash when no live sessions', async () => {
    await renderWith({ liveSessions: { active: 0, idle: 0 } })
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('marks the next pick with #1 next and a reason tooltip', async () => {
    await renderWith({ rank: 1, rankReason: 'perishable quota, lowest load' })
    expect(screen.getByText('#1 next')).toHaveAttribute('title', 'perishable quota, lowest load')
  })

  it('renders a plain rank number for non-first picks', async () => {
    await renderWith({ rank: 2, rankReason: 'fresh quota — saved for last' })
    expect(screen.getByText('#2')).toBeInTheDocument()
  })

  it('shows unavailable when an account has no rank', async () => {
    await renderWith({ rank: null })
    expect(screen.getByText('unavailable')).toBeInTheDocument()
  })
})
