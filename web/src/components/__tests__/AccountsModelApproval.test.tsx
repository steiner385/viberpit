/**
 * AccountsManager — per-account model-approval override select.
 *
 * Moved here from `components/settings/__tests__/ModelApprovalControls.test.tsx`
 * when the legacy `GeneralSection` was deleted (S34). The global half of that
 * file (action select + denylist) now lives in
 * `features/settings/sections/SessionDefaultsSection.test.tsx`; this half still
 * describes `AccountsManager`, so it moves next to its siblings instead.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(),
  useSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useCreateSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRenameSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRefreshAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useUpdateAccount: vi.fn(),
  useDeleteAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useGithubRepos: vi.fn(() => ({ data: [], isLoading: false, isError: false })),
  useGithubBranches: vi.fn(() => ({ data: { default: null, branches: [] }, isLoading: false })),
  useModels: vi.fn(() => ({ data: [{ id: 'default', label: 'Default' }] })),
  useSettings: vi.fn(() => ({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false })),
  useUpdateSettings: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSubmitReauthCode: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useArchivedSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useUnarchiveSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useBulkUnarchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useBulkPermanentDeleteSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSendPrompt: () => ({ mutate: vi.fn() }),
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useSessionsViewing: vi.fn(() => ({ data: {} })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: { accounts: ['accounts'], sessions: ['sessions'], archived: ['archived-sessions'], config: ['config'], settings: ['settings'] },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
  isSuccess: false,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings?section=accounts']}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

describe('AccountsManager — per-account model approval override', () => {
  const ACCOUNT = {
    name: 'alice@example.com',
    status: 'ok' as const,
    usage: null,
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    accountDefaults: {
      defaultModel: null,
      defaultPermissionMode: null,
      envOverrides: null,
      modelApprovalAction: null,
    },
  }

  beforeEach(() => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [ACCOUNT],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    vi.mocked(hooks.useUpdateAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  })

  it('renders the model approval select for each account', () => {
    render(<AccountsManager />, { wrapper })
    expect(screen.getByLabelText(/model approval action for alice@example\.com/i)).toBeInTheDocument()
  })

  it('defaults to Inherit when accountDefaults.modelApprovalAction is null', () => {
    render(<AccountsManager />, { wrapper })
    const select = screen.getByLabelText(/model approval action for alice@example\.com/i) as HTMLSelectElement
    expect(select.value).toBe('')
  })

  it('pre-fills from accountDefaults when non-null', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [{ ...ACCOUNT, accountDefaults: { ...ACCOUNT.accountDefaults!, modelApprovalAction: 'allow' } }],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    render(<AccountsManager />, { wrapper })
    const select = screen.getByLabelText(/model approval action for alice@example\.com/i) as HTMLSelectElement
    expect(select.value).toBe('allow')
  })

  it('calls updateAccount with modelApprovalAction on change', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.selectOptions(
      screen.getByLabelText(/model approval action for alice@example\.com/i),
      'require_approval',
    )
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'alice@example.com', modelApprovalAction: 'require_approval' }),
    )
  })

  it('sends null for modelApprovalAction when Inherit is selected', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [{ ...ACCOUNT, accountDefaults: { ...ACCOUNT.accountDefaults!, modelApprovalAction: 'block' } }],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.selectOptions(
      screen.getByLabelText(/model approval action for alice@example\.com/i),
      '',
    )
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'alice@example.com', modelApprovalAction: null }),
    )
  })
})
