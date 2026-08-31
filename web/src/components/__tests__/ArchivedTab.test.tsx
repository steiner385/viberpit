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
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
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
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSendPrompt: () => ({ mutate: vi.fn() }),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useAllPrs: vi.fn(() => ({ data: [] })),
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
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
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

// ── Tab presence and switching ────────────────────────────────────────────────

describe('Dashboard — Archived tab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('renders Sessions, Settings, and Archived tabs', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('tab', { name: /sessions/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /settings/i })).toBeInTheDocument()
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

  // ── Arrow-key nav cycles the tab strip ───────────────────────────────────────

  it('ArrowRight from Sessions moves focus to Pipeline', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /sessions/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /pipeline/i }))
  })

  it('ArrowRight from Pipeline moves focus to Automations', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /pipeline/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /automations/i }))
  })

  it('ArrowRight from Archived moves focus to Insights', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /archived/i }).focus()
    await user.keyboard('{ArrowRight}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /insights/i }))
  })

  it('ArrowLeft from Archived moves focus to Initiatives', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    screen.getByRole('tab', { name: /archived/i }).focus()
    await user.keyboard('{ArrowLeft}')

    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /initiatives/i }))
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

  // ── Bulk operations ───────────────────────────────────────────────────────────

  function twoArchived() {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [
        makeArchived({ id: 'arc-1', name: 'Alpha' }),
        makeArchived({ id: 'arc-2', name: 'Beta' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)
  }

  function fourArchived() {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [
        makeArchived({ id: 'arc-1', name: 'Alpha' }),
        makeArchived({ id: 'arc-2', name: 'Beta' }),
        makeArchived({ id: 'arc-3', name: 'Gamma' }),
        makeArchived({ id: 'arc-4', name: 'Delta' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)
  }

  async function shiftClick(user: ReturnType<typeof userEvent.setup>, el: HTMLElement) {
    await user.keyboard('{Shift>}')
    await user.click(el)
    await user.keyboard('{/Shift}')
  }

  it('selecting a row reveals the bulk action bar with a count', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))

    const bar = screen.getByRole('region', { name: /bulk actions/i })
    expect(bar).toHaveTextContent(/1 selected/i)
  })

  it('Select all selects every row', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/2 selected/i)
  })

  it('bulk Unarchive calls useBulkUnarchiveSessions.mutate with selected ids', async () => {
    const user = userEvent.setup()
    const bulkMutate = vi.fn()
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue({
      mutate: bulkMutate, isPending: false,
    } as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('checkbox', { name: /select beta/i }))
    await user.click(screen.getByRole('button', { name: /unarchive \(2\)/i }))

    expect(bulkMutate).toHaveBeenCalledWith(
      expect.arrayContaining(['arc-1', 'arc-2']),
      expect.anything(),
    )
  })

  it('bulk Delete opens the confirm dialog and calls bulk delete on confirm', async () => {
    const user = userEvent.setup()
    const bulkDeleteMutate = vi.fn()
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: bulkDeleteMutate, isPending: false,
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('button', { name: /^delete \(1\)/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))
    expect(bulkDeleteMutate).toHaveBeenCalledWith(['arc-1'], expect.anything())
  })

  it('shift-clicking a second row selects the whole range between the two clicks', async () => {
    const user = userEvent.setup()
    fourArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    // Anchor on Alpha (index 0), then shift-click Gamma (index 2) → Alpha, Beta, Gamma.
    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await shiftClick(user, screen.getByRole('checkbox', { name: /select gamma/i }))

    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/3 selected/i)
    expect(screen.getByRole('checkbox', { name: /select alpha/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select beta/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select gamma/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select delta/i })).not.toBeChecked()
  })

  it('shift-click range works upward (anchor below the shift-clicked row)', async () => {
    const user = userEvent.setup()
    fourArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    // Anchor on Delta (index 3), then shift-click Beta (index 1) → Beta, Gamma, Delta.
    await user.click(screen.getByRole('checkbox', { name: /select delta/i }))
    await shiftClick(user, screen.getByRole('checkbox', { name: /select beta/i }))

    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/3 selected/i)
    expect(screen.getByRole('checkbox', { name: /select alpha/i })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select beta/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select gamma/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /select delta/i })).toBeChecked()
  })

  it('shift-clicking with no prior selection just selects the single row', async () => {
    const user = userEvent.setup()
    fourArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await shiftClick(user, screen.getByRole('checkbox', { name: /select gamma/i }))

    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/1 selected/i)
    expect(screen.getByRole('checkbox', { name: /select gamma/i })).toBeChecked()
  })

  it('Clear deselects all and hides the action bar', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('button', { name: /clear/i }))
    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()
  })
})
