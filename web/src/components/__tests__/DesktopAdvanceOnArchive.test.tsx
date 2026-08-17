// web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DesktopShell } from '../DesktopShell'
import type { Session } from '@/api/types'

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const DEFAULT_SESSIONS: Session[] = [
  { id: 'sess-1', name: 'First', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
  { id: 'sess-2', name: 'Second', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
]
// Reassigned per-test (useSessions initialises its useState from SESSIONS).
let SESSIONS: Session[] = DEFAULT_SESSIONS

// Listeners registered by the reactive useSessions mock — used by pushSessions
// to trigger a mid-test re-render without onSuccess firing (error-path tests).
const _sessionsListeners = new Set<(s: Session[]) => void>()

// Simulate the server confirming a session left the live list (e.g. after a
// 503/409 archive that succeeded despite the error response).
function pushSessions(sessions: Session[]) {
  SESSIONS = sessions
  _sessionsListeners.forEach((cb) => cb([...sessions]))
}

// When true  (default) useCloseSession.mutate calls opts.onSuccess synchronously
// — the happy path. Set to false to simulate the error path where onError fires
// instead of onSuccess, so the per-call onDone callback is never invoked.
let _closeSessionCallsOnSuccess = true

// closeSession fires onSuccess synchronously so the host's onDone runs in-test.
// permanentDeleteSession also fires onSuccess synchronously for the delete-path test.
vi.mock('@/api/hooks', async (importOriginal) => {
  const { useState, useEffect } = await import('react')
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    useAccounts: () => ({ data: [{ name: 'alice', status: 'ok', usage: { session: { pct: 0, resetsAt: null }, weekly: { pct: 0, resetsAt: null } }, liveSessions: { active: 0, idle: 0 }, rank: 1, rankReason: null }] }),
    // Reactive: initialised from SESSIONS (current at first render), then updated
    // via pushSessions() for mid-test session-list changes.
    useSessions: () => {
      const [sessions, setSessions] = useState<Session[]>(SESSIONS)
      useEffect(() => {
        _sessionsListeners.add(setSessions)
        return () => { _sessionsListeners.delete(setSessions) }
      }, [])
      return { data: sessions, isLoading: false }
    },
    useArchivedSessions: () => ({ data: [] }),
    useGithubRepos: () => ({ data: [], isLoading: false, isError: false }),
    useCreateSession: () => ({ mutate: vi.fn(), isPending: false }),
    useRenameSession: () => ({ mutate: vi.fn(), isPending: false }),
    usePermanentDeleteSession: () => ({ mutate: (_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.(), isPending: false }),
    useSendPrompt: () => ({ mutate: vi.fn() }),
    useCloseSession: () => ({
      mutate: (_id: string, opts?: { onSuccess?: () => void }) => {
        if (_closeSessionCallsOnSuccess) opts?.onSuccess?.()
      },
      isPending: false,
    }),
    useBulkArchiveSessions: () => ({ mutate: vi.fn(), isPending: false }),
    useSessionsViewing: vi.fn(),
    useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
    useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
    useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  }
})

// Keep the runtime subtree inert; the title comes from context.
vi.mock('../session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useSessionRuntimeContext: () => ({ title: 'Open', commandResult: null, commands: [], dismissCommandResult: vi.fn() }),
}))
vi.mock('../session/SessionBody', () => ({ SessionBody: () => <div /> }))
vi.mock('../session/SessionComposer', () => ({ SessionComposer: () => <div /> }))
vi.mock('../session/CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear() // default 'live' ordering: groupBy none, sortBy status
  SESSIONS = DEFAULT_SESSIONS
  _sessionsListeners.clear()
  _closeSessionCallsOnSuccess = true
})

describe('DesktopShell — advance on archive', () => {
  it('advances the detail selection to the neighbor when the open session is archived', async () => {
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    // Open sess-1 from the sidebar, then archive it from the detail header.
    await user.click(screen.getByRole('button', { name: /open session first/i }))
    await user.click(screen.getByRole('button', { name: /archive session open/i }))

    // sess-2 (the row that takes its place) is now the selected row.
    expect(document.getElementById('row-sess-2')).toHaveAttribute('data-selected', 'true')
  })

  it('does not move selection when a non-open session is archived from the sidebar', async () => {
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    // Open sess-1 in the detail pane.
    await user.click(screen.getByRole('button', { name: /open session first/i }))
    expect(document.getElementById('row-sess-1')).toHaveAttribute('data-selected', 'true')

    // Archive sess-2 via its sidebar card archive button — handleClose('sess-2') fires,
    // but the guard (cur.id === id) is false because cur.id is 'sess-1', so selection
    // must remain on sess-1.
    await user.click(screen.getByRole('button', { name: /archive session second/i }))

    expect(document.getElementById('row-sess-1')).toHaveAttribute('data-selected', 'true')
    expect(document.getElementById('row-sess-2')).not.toHaveAttribute('data-selected', 'true')
  })

  it('advances the detail selection to the neighbor when the open session is permanently deleted', async () => {
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    // Open sess-1 from the sidebar.
    await user.click(screen.getByRole('button', { name: /open session first/i }))
    expect(document.getElementById('row-sess-1')).toHaveAttribute('data-selected', 'true')

    // Open the "More session actions" overflow menu in the detail header.
    await user.click(screen.getByRole('button', { name: /more session actions/i }))

    // Click "Delete permanently" in the dropdown menu.
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))

    // Confirm in the PermanentDeleteDialog.
    // Both useCloseSession and usePermanentDeleteSession fire onSuccess synchronously,
    // so onDone() → setSelection(advanceFrom('sess-1')) runs immediately.
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    // sess-2 is now the selected row.
    expect(document.getElementById('row-sess-2')).toHaveAttribute('data-selected', 'true')
  })

  it("advances via the full ordering when the open session's group is collapsed", async () => {
    // Group by status: sess-1 (idle → "Done" tier) and sess-2 (in_progress →
    // "Working" tier) land in different groups.
    localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'status' }))
    SESSIONS = [
      { id: 'sess-1', name: 'First', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
      { id: 'sess-2', name: 'Second', accountName: 'alice', status: 'in_progress', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
    ]
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    // Open sess-1, then collapse its "Done" group — its row leaves the visible
    // list while the session stays open in the detail pane.
    await user.click(screen.getByRole('button', { name: /open session first/i }))
    await user.click(screen.getByRole('button', { name: /done/i }))
    expect(document.getElementById('row-sess-1')).toBeNull()

    // Archiving from the detail header must still advance to sess-2, even though
    // the archived session is no longer in the visible ordering.
    await user.click(screen.getByRole('button', { name: /archive session open/i }))
    expect(document.getElementById('row-sess-2')).toHaveAttribute('data-selected', 'true')
  })

  it('advances via fallback effect when archive API returns an error (onSuccess never fires)', async () => {
    // Simulate the 503/409 error path: useCloseSession fires onError instead of
    // onSuccess, so the per-call { onSuccess: onDone } callback in SessionActions
    // is never invoked. The session later disappears from the live list when
    // onSettled confirms the archive took effect. The fallback useEffect in
    // DesktopShell should advance the selection to the neighbor.
    _closeSessionCallsOnSuccess = false

    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    // Open sess-1.
    await user.click(screen.getByRole('button', { name: /open session first/i }))
    expect(document.getElementById('row-sess-1')).toHaveAttribute('data-selected', 'true')

    // Click archive — mutate fires but onSuccess is NOT called; selection stays put.
    await user.click(screen.getByRole('button', { name: /archive session open/i }))
    expect(document.getElementById('row-sess-1')).toHaveAttribute('data-selected', 'true')

    // Simulate onSettled confirming the archive: sess-1 leaves the live list.
    await act(async () => {
      pushSessions([DEFAULT_SESSIONS[1]])
    })

    // The fallback useEffect should have advanced selection to sess-2 (the
    // neighbor, computed from the pre-removal snapshot in selectionOrderRef).
    expect(document.getElementById('row-sess-2')).toHaveAttribute('data-selected', 'true')
  })
})
