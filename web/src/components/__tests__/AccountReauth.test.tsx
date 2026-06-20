import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'
import type { ReauthState } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(),
  useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    config: ['config'],
    githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo],
    settings: ['settings'],
  },
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'kindash',
    status: 'needs-relogin',
    usage: null,
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
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
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
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({
    data: makeReauthState({ state: 'idle' }),
    isLoading: false,
  } as ReturnType<typeof hooks.useReauthStatus>)
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

  it('shows "Re-authenticate" button for needs-relogin account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('shows "Re-authenticate" button for needs-reauth account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-reauth' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('button', { name: /re-authenticate kindash/i })).toBeInTheDocument()
  })

  it('does not show "Re-authenticate" button for ok account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'ok', usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.queryByRole('button', { name: /re-authenticate kindash/i })).not.toBeInTheDocument()
  })

  it('clicking Re-authenticate calls the POST hook', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /re-authenticate kindash/i }))

    expect(reauthMutate).toHaveBeenCalledWith('kindash', expect.anything())
  })

  it('shows "on the host machine" in the status line when running', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText(/on the host machine/i)).toBeInTheDocument()
  })

  it('shows Cancel button while running', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'running' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })

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

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /cancel reauth/i }))

    expect(cancelMutate).toHaveBeenCalledWith('kindash', expect.anything())

    // Optimistically: the status line should vanish (back to idle locally)
    await waitFor(() => {
      expect(screen.queryByText(/on the host machine/i)).not.toBeInTheDocument()
    })
  })

  it('shows success indicator (✓) when status is success', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'success' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })

    // Should show a success indicator with checkmark
    expect(screen.getByText(/✓/)).toBeInTheDocument()
  })

  it('shows error message when status is error', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'error', message: 'Login failed: timeout' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText(/Login failed: timeout/i)).toBeInTheDocument()
  })

  it('shows error message when status is timeout', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({
      data: makeReauthState({ state: 'timeout', message: 'Re-authentication timed out after 5 minutes.' }),
      isLoading: false,
    } as ReturnType<typeof hooks.useReauthStatus>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText(/timed out/i)).toBeInTheDocument()
  })
})
