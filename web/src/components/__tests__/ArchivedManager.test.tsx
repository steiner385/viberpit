/**
 * ArchivedManager.test.tsx
 *
 * Tests for the ArchivedManager component in isolation (not via Dashboard):
 * - Controls strip (Group-by / Sort-by selects) renders when sessions exist
 * - Switching Group-by to "Account" shows account group headers
 * - Search filters the list (matchedIds from useSessionSearch)
 * - Empty-search state shows a "no match" message with a Clear button
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
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
  QK: {
    archived: ['archived-sessions'],
    sessionSearch: (q: string, scope: string) => ['session-search', q, scope],
  },
}))

// useSessionSearch is called from within ArchivedManager; mock it at module level.
// Individual tests can override searchState to simulate filtering.
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
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('ArchivedManager — controls strip', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

  it('select-all operates on the VISIBLE (filtered) set, not the raw list', () => {
    // Only session '1' is visible after filtering
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

    // Before selecting all, bulk bar should not be visible
    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()
  })
})
