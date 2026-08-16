/**
 * ArchivedManager.test.tsx
 *
 * Tests for the ArchivedManager component in isolation (now the single archived
 * list, used by both DesktopShell and the Dashboard archived tab):
 * - Controls strip (Group-by / Sort-by selects) renders when sessions exist
 * - Switching Group-by to "Account" shows account group headers
 * - Search filters the list (matchedIds from useSessionSearch)
 * - Empty-search state shows a "no match" message with a Clear button
 * - select-all operates on the VISIBLE (filtered) set
 * - shift-click range selection over the visible order (from #179)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { ArchivedManager } from '../ArchivedManager'
import * as hooks from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'

// ── Global mocks ──────────────────────────────────────────────────────────────

vi.mock('@/api/hooks', () => ({
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {
    archived: ['archived-sessions'],
    sessions: ['sessions'],
    sessionSearch: (q: string, scope: string) => ['session-search', q, scope],
  },
}))

// useSessionSearch is called from within ArchivedManager; mock it at module level.
// Individual tests override searchState to simulate filtering; both describe blocks
// reset it in beforeEach so a filtered state never leaks across tests.
let searchState = { query: '', setQuery: vi.fn(), matchedIds: null as Set<string> | null, isFiltered: false }

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => searchState,
}))

// ── Helpers ───────────────────────────────────────────────────────────────────

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false }

function makeArchived(overrides: Partial<ArchivedSession> = {}): ArchivedSession {
  return {
    id: 'arc-1',
    name: 'Alpha',
    account: 'tony',
    cwd: '/home/GitHub/KinDash',
    claudeSessionId: null,
    permissionMode: 'default',
    createdAt: '2026-06-01T00:00:00Z',
    archivedAt: '2026-06-10T00:00:00Z',
    ...overrides,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupMocks(sessions: ArchivedSession[]) {
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: sessions, isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({ ...NOOP_MUTATION, progress: null, cancel: vi.fn() } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

// Four sessions sharing the default archivedAt → the default 'archived' sort
// tie-breaks by id, so the visible order is arc-1..arc-4 (Alpha, Beta, Gamma, Delta).
function fourArchived() {
  return [
    makeArchived({ id: 'arc-1', name: 'Alpha' }),
    makeArchived({ id: 'arc-2', name: 'Beta' }),
    makeArchived({ id: 'arc-3', name: 'Gamma' }),
    makeArchived({ id: 'arc-4', name: 'Delta' }),
  ]
}

async function shiftClick(user: ReturnType<typeof userEvent.setup>, el: HTMLElement) {
  await user.keyboard('{Shift>}')
  await user.click(el)
  await user.keyboard('{/Shift}')
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('ArchivedManager — controls strip', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    searchState = { query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }
  })

  it('does NOT render controls when there are no archived sessions', () => {
    setupMocks([])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    expect(screen.queryByLabelText('Group by')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Sort by')).not.toBeInTheDocument()
  })

  it('renders Group-by and Sort-by controls when sessions exist', () => {
    setupMocks([makeArchived()])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    expect(screen.getByLabelText('Group by')).toBeInTheDocument()
    expect(screen.getByLabelText('Sort by')).toBeInTheDocument()
  })

  it('row metadata shows the archive date AND time, not just the date', () => {
    const archivedAt = '2026-06-10T14:37:00Z'
    setupMocks([makeArchived({ id: '1', name: 'Alpha', archivedAt })])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    // Compute the expectation with the same formatter the component uses, so the
    // assertion is timezone-agnostic (toLocaleString renders in the runner's TZ).
    const expected = new Date(archivedAt).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
    // A time component (HH:MM) must be present — guards against a regression to a
    // date-only format (toLocaleDateString silently drops hour/minute options).
    expect(expected).toMatch(/\d{1,2}:\d{2}/)
    // Match the metadata <p> itself (ancestor <div>s also contain the substring
    // via textContent, so restrict by tagName to keep the query unambiguous).
    expect(
      screen.getByText(
        (_, el) => el?.tagName === 'P' && (el.textContent?.includes(`Archived ${expected}`) ?? false),
      ),
    ).toBeInTheDocument()
  })

  it('switching Group-by to "Account" shows account group headers', async () => {
    const user = userEvent.setup()
    setupMocks([
      makeArchived({ id: '1', name: 'Alpha', account: 'tony' }),
      makeArchived({ id: '2', name: 'Beta', account: 'work', cwd: '/home/GitHub/infra', archivedAt: '2026-06-12T00:00:00Z' }),
    ])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    await user.selectOptions(screen.getByLabelText('Group by'), 'account')

    expect(screen.getByText('tony')).toBeInTheDocument()
    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('search filters the archived list to matching sessions only', () => {
    // Mock search returning only id='1' (Alpha)
    searchState = {
      query: 'alpha',
      setQuery: vi.fn(),
      matchedIds: new Set(['1']),
      isFiltered: true,
    }
    setupMocks([
      makeArchived({ id: '1', name: 'Alpha', account: 'tony' }),
      makeArchived({ id: '2', name: 'Beta', account: 'work', cwd: '/home/GitHub/infra', archivedAt: '2026-06-12T00:00:00Z' }),
    ])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByText('Beta')).not.toBeInTheDocument()
  })

  it('shows "no match" empty state when search yields zero results', () => {
    searchState = {
      query: 'zzz',
      setQuery: vi.fn(),
      matchedIds: new Set([]),    // empty match set — no results
      isFiltered: true,
    }
    setupMocks([
      makeArchived({ id: '1', name: 'Alpha', account: 'tony' }),
    ])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    expect(screen.getByText(/no archived sessions match your search/i)).toBeInTheDocument()
    // The empty-state Clear button has no aria-label; locate via its text content
    expect(screen.getAllByRole('button').some((b) => b.textContent === 'Clear')).toBe(true)
  })

  it('Clear button in empty-search state calls search.setQuery with empty string', async () => {
    const user = userEvent.setup()
    const setQuery = vi.fn()
    searchState = {
      query: 'zzz',
      setQuery,
      matchedIds: new Set([]),
      isFiltered: true,
    }
    setupMocks([makeArchived({ id: '1', name: 'Alpha' })])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    // Two "Clear"-ish buttons exist: "Clear search" (aria-label) in the search box, and
    // the plain "Clear" text button in the empty-state message. Target the plain-text one.
    const clearBtn = screen.getAllByRole('button').find((b) => b.textContent === 'Clear')
    if (!clearBtn) throw new Error('Clear button not found in empty-state')
    await user.click(clearBtn)

    expect(setQuery).toHaveBeenCalledWith('')
  })

  it('select-all operates on the VISIBLE (filtered) set, not the raw list', async () => {
    const user = userEvent.setup()
    // Only session '1' is visible after filtering; '2' is hidden
    searchState = {
      query: 'alpha',
      setQuery: vi.fn(),
      matchedIds: new Set(['1']),
      isFiltered: true,
    }
    setupMocks([
      makeArchived({ id: '1', name: 'Alpha' }),
      makeArchived({ id: '2', name: 'Beta', cwd: '/home/GitHub/infra', archivedAt: '2026-06-12T00:00:00Z' }),
    ])
    render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })

    // Before selecting, bulk bar must not be visible
    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()

    // Click the "Select all" checkbox — only the ONE visible row should be selected
    await user.click(screen.getByRole('checkbox', { name: /select all/i }))

    // BulkActionBar shows "1 selected" — NOT "2 selected" (hidden row excluded)
    await waitFor(() => {
      expect(screen.getByRole('region', { name: /bulk actions/i })).toBeInTheDocument()
    })
    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent('1 selected')
  })
})

describe('ArchivedManager — shift-click range selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    searchState = { query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }
  })

  it('shift-clicking a second row selects the whole range between the two clicks', async () => {
    const user = userEvent.setup()
    setupMocks(fourArchived())
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

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
    setupMocks(fourArchived())
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

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
    setupMocks(fourArchived())
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await shiftClick(user, screen.getByRole('checkbox', { name: /select gamma/i }))

    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/1 selected/i)
    expect(screen.getByRole('checkbox', { name: /select gamma/i })).toBeChecked()
  })
})

describe('ArchivedManager — bulk delete progress', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    searchState = { query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }
  })

  it('keeps the frozen count after the archived list empties mid-delete', async () => {
    const user = userEvent.setup()
    setupMocks(fourArchived())
    const { rerender } = render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))
    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()

    // Simulate the optimistic cache emptying out from under the open dialog.
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
    rerender(<ArchivedManager onReviewSession={vi.fn()} />)

    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()
  })

  it('renders a progress bar with "X of N" while deleting', async () => {
    const user = userEvent.setup()
    setupMocks(fourArchived())
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: vi.fn(), isPending: true, progress: { done: 2, total: 4 }, cancel: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))

    const bar = screen.getByRole('progressbar')
    expect(bar).toBeInTheDocument()
    expect(bar).toHaveAttribute('aria-valuenow', '2')
    expect(bar).toHaveAttribute('aria-valuemax', '4')
    expect(screen.getByText(/2 of 4/)).toBeInTheDocument()
  })

  it('Cancel during a delete calls the hook cancel(), not close', async () => {
    const user = userEvent.setup()
    const cancel = vi.fn()
    setupMocks(fourArchived())
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: vi.fn(), isPending: true, progress: { done: 1, total: 4 }, cancel,
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))
    await user.click(screen.getByRole('button', { name: /^cancel$/i }))

    expect(cancel).toHaveBeenCalled()
  })
})

describe('ArchivedManager — quick-purge shortcuts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    searchState = { query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }
  })

  // N archived rows with strictly increasing archivedAt, so arc-0 is oldest and
  // the "oldest 25" are arc-0..arc-24.
  function manyArchived(n: number): ArchivedSession[] {
    return Array.from({ length: n }, (_, i) =>
      makeArchived({
        id: `arc-${i}`,
        name: `S${i}`,
        archivedAt: `2026-06-10T00:${String(i).padStart(2, '0')}:00Z`,
      }),
    )
  }

  it('renders the four purge-oldest shortcut buttons when sessions exist', () => {
    setupMocks(fourArchived())
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    for (const n of [25, 50, 100, 200]) {
      expect(
        screen.getByRole('button', { name: new RegExp(`purge the ${n} oldest`, 'i') }),
      ).toBeInTheDocument()
    }
  })

  it('does NOT render the quick-purge bar when there are no archived sessions', () => {
    setupMocks([])
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })
    expect(screen.queryByRole('region', { name: /quick purge/i })).not.toBeInTheDocument()
  })

  it('disables a shortcut when fewer than N archived sessions exist', () => {
    setupMocks(fourArchived()) // only 4 archived
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    // All four thresholds exceed 4 → every button disabled.
    for (const n of [25, 50, 100, 200]) {
      expect(
        screen.getByRole('button', { name: new RegExp(`purge the ${n} oldest`, 'i') }),
      ).toBeDisabled()
    }
  })

  it('enables the 25 shortcut once ≥25 archived exist and it opens the bulk dialog', async () => {
    const user = userEvent.setup()
    setupMocks(manyArchived(30))
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    const purge25 = screen.getByRole('button', { name: /purge the 25 oldest/i })
    expect(purge25).toBeEnabled()
    // 50/100/200 still disabled with only 30 archived.
    expect(screen.getByRole('button', { name: /purge the 50 oldest/i })).toBeDisabled()

    await user.click(purge25)
    expect(screen.getByText('Permanently delete 25 sessions')).toBeInTheDocument()
  })

  it('purges exactly the oldest N (by archivedAt) on confirm', async () => {
    const user = userEvent.setup()
    const mutate = vi.fn()
    setupMocks(manyArchived(30))
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate, isPending: false, progress: null, cancel: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('button', { name: /purge the 25 oldest/i }))
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(mutate).toHaveBeenCalledTimes(1)
    const ids = mutate.mock.calls[0][0] as string[]
    expect(ids).toHaveLength(25)
    // Oldest 25 are arc-0..arc-24; arc-25..arc-29 are the 5 newest and must be spared.
    expect(ids).toContain('arc-0')
    expect(ids).toContain('arc-24')
    expect(ids).not.toContain('arc-25')
    expect(ids).not.toContain('arc-29')
  })
})
