import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import { toast } from 'sonner'
import type { Account, Session } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
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

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'work',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    ...overrides,
  }
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    name: 'My Session',
    accountName: 'work',
    status: 'idle',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
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
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

describe('Dashboard — all-limited banner + optimistic toasts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('shows a subtle toast when a session is created optimistically', async () => {
    const user = userEvent.setup()
    const session = makeSession({ usageUnverified: true })
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    render(<Dashboard />, { wrapper })

    // Open new session dialog
    const newButtons = screen.getAllByRole('button', { name: /new session/i })
    await user.click(newButtons[0])

    // Submit the dialog form (the submit button inside the dialog)
    const submitBtn = screen.getByRole('button', { name: /^create$/i })
    await user.click(submitBtn)

    act(() => { capturedCallbacks.onSuccess?.(session) })

    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.stringContaining('started anyway'),
    )
  })

  it('routes accounts_need_reauth 409 to a toast, not the limit banner', async () => {
    const user = userEvent.setup()
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    render(<Dashboard />, { wrapper })

    const newButtons = screen.getAllByRole('button', { name: /new session/i })
    await user.click(newButtons[0])

    const submitBtn = screen.getByRole('button', { name: /^create$/i })
    await user.click(submitBtn)

    act(() => {
      capturedCallbacks.onError?.({ status: 409, body: { error: { code: 'accounts_need_reauth' } } })
    })

    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      expect.stringContaining('re-authenticate'),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('auto-clears the all-limited banner once an account becomes selectable', async () => {
    const user = userEvent.setup()
    let capturedCallbacks: { onSuccess?: (s: Session) => void; onError?: (e: { status: number; body: unknown }) => void } = {}
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_input, callbacks) => { capturedCallbacks = callbacks ?? {} }),
      isPending: false,
      isSuccess: false,
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    const { rerender } = render(<Dashboard />, { wrapper })

    const newButtons = screen.getAllByRole('button', { name: /new session/i })
    await user.click(newButtons[0])

    const submitBtn = screen.getByRole('button', { name: /^create$/i })
    await user.click(submitBtn)

    act(() => {
      capturedCallbacks.onError?.({ status: 409, body: { error: { code: 'all_accounts_limited' }, earliestReset: null } })
    })

    // Banner should now be visible
    expect(screen.getByRole('alert')).toBeInTheDocument()

    // Simulate account recovery (rank becomes non-null)
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ rank: 1 })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    rerender(<Dashboard />)

    // Auto-clear effect should remove the banner
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
