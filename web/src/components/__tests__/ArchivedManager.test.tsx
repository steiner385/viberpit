/**
 * ArchivedManager.test.tsx
 *
 * Tests for the desktop archived-sessions panel (used by DesktopShell).
 * Mirrors the bulk-selection coverage in ArchivedTab.test.tsx — in particular
 * shift-click range selection — since ArchivedManager carries its own copy of
 * the archived list until the two are consolidated.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ArchivedManager } from '../ArchivedManager'
import * as hooks from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  QK: { archived: ['archived-sessions'], sessions: ['sessions'] },
}))

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }

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

function setupMocks(sessions: ArchivedSession[]) {
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: sessions, isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

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

describe('ArchivedManager — shift-click range selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
