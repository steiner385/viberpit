/**
 * ArchivedTab.test.tsx
 *
 * Tests for the Archived tab in Dashboard:
 * - Tab renders and switches
 * - Arrow-key nav cycles through all 3 tabs
 * - Archived list rows render with name/account/date
 * - Review calls onReviewSession
 * - Unarchive calls the hook
 * - Delete permanently opens confirm dialog, calls hook on confirm, cancel closes without calling
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
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

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeArchived(overrides: Partial<ArchivedSession> = {}): ArchivedSession {
  return {
    id: 'arc-1',
    name: 'My Archived Session',
    account: 'work',
    cwd: '/home/user/project',
    claudeSessionId: 'claude-abc',
    permissionMode: 'interactive',
    createdAt: '2026-06-01T10:00:00Z',
    archivedAt: '2026-06-20T12:00:00Z',
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
}

// ── Tab presence and switching ────────────────────────────────────────────────

describe('Dashboard — Archived tab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('renders Sessions, Accounts, and Archived tabs', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('tab', { name: /sessions/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /accounts/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /archived/i })).toBeInTheDocument()
  })

  it('Archived tab is NOT selected by default', () => {
    render(<Dashboard />, { wrapper })

    const archivedTab = screen.getByRole('tab', { name: /archived/i })
    expect(archivedTab).toHaveAttribute('aria-selected', 'false')
  })

  it('clicking Archived tab switches to the Archived panel', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    const tab = screen.getByRole('tab', { name: /archived/i })
    expect(tab).toHaveAttribute('aria-selected', 'true')

    expect(screen.getByText(/no archived sessions/i)).toBeInTheDocument()
  })

  // ── Arrow-key nav cycles all 3 tabs ──────────────────────────────────────────

  it('ArrowRight from Sessions moves focus to Accounts', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /sessions/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /accounts/i }))
  })

  it('ArrowRight from Accounts moves focus to Archived', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /accounts/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /archived/i }))
  })

  it('ArrowRight from Archived wraps back to Sessions', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /archived/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /sessions/i }))
  })

  it('ArrowLeft from Sessions wraps to Archived', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /sessions/i }).focus()
    await user.keyboard('{ArrowLeft}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /archived/i }))
  })

  // ── Archived list rows ───────────────────────────────────────────────────────

  it('renders archived rows with name, account, and archived date', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ name: 'Alpha', account: 'myaccount', archivedAt: '2026-06-15T00:00:00Z' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText(/myaccount/)).toBeInTheDocument()
    // Date should be formatted (just check the year is present)
    expect(screen.getByText(/2026/)).toBeInTheDocument()
  })

  it('falls back to short id when name is empty', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'abc12345def', name: '' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    expect(screen.getByText('abc12345')).toBeInTheDocument()
  })

  it('shows count badge when archived sessions exist', () => {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived(), makeArchived({ id: 'arc-2', name: 'Beta' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })

    // Badge "2" should appear in the tab
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  // ── Review button ─────────────────────────────────────────────────────────────

  it('clicking Review calls onReviewSession with the session id', async () => {
    const user = userEvent.setup()
    const onReview = vi.fn()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard onReviewSession={onReview} />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /review alpha/i }))

    expect(onReview).toHaveBeenCalledWith('arc-1')
  })

  // ── Unarchive button ──────────────────────────────────────────────────────────

  it('clicking Unarchive calls useUnarchiveSession.mutate with the id', async () => {
    const user = userEvent.setup()
    const unarchiveMutate = vi.fn()
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue({
      mutate: unarchiveMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /unarchive alpha/i }))

    expect(unarchiveMutate).toHaveBeenCalledWith('arc-1', expect.anything())
  })

  // ── Delete permanently ────────────────────────────────────────────────────────

  it('clicking Delete permanently opens a confirm dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /delete alpha permanently/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /permanently delete/i })).toBeInTheDocument()
  })

  it('confirming the permanent delete calls the mutation', async () => {
    const user = userEvent.setup()
    const permDeleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({
      mutate: permDeleteMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /delete alpha permanently/i }))

    const confirmBtn = screen.getByRole('button', { name: /delete permanently/i })
    await user.click(confirmBtn)

    expect(permDeleteMutate).toHaveBeenCalledWith('arc-1', expect.anything())
  })

  it('Cancel in the permanent-delete dialog closes without calling the mutation', async () => {
    const user = userEvent.setup()
    const permDeleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({
      mutate: permDeleteMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /delete alpha permanently/i }))

    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })
    expect(permDeleteMutate).not.toHaveBeenCalled()
  })

  it('Escape key closes the permanent-delete dialog without calling the mutation', async () => {
    const user = userEvent.setup()
    const permDeleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({
      mutate: permDeleteMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [makeArchived({ id: 'arc-1', name: 'Alpha' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)

    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))
    await user.click(screen.getByRole('button', { name: /delete alpha permanently/i }))

    await user.keyboard('{Escape}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })
    expect(permDeleteMutate).not.toHaveBeenCalled()
  })
})
