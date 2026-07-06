// web/src/components/__tests__/DesktopAdvanceOnArchive.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DesktopShell } from '../DesktopShell'
import type { Session } from '@/api/types'

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const SESSIONS: Session[] = [
  { id: 'sess-1', name: 'First', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
  { id: 'sess-2', name: 'Second', accountName: 'alice', status: 'idle', createdAt: '2026-06-19T10:00:00Z', updatedAt: '2026-06-19T10:00:00Z' },
]

// closeSession fires onSuccess synchronously so the host's onDone runs in-test.
// permanentDeleteSession also fires onSuccess synchronously for the delete-path test.
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    useAccounts: () => ({ data: [{ name: 'alice', status: 'ok', usage: { session: { pct: 0, resetsAt: null }, weekly: { pct: 0, resetsAt: null } }, liveSessions: { active: 0, idle: 0 }, rank: 1, rankReason: null }] }),
    useSessions: () => ({ data: SESSIONS, isLoading: false }),
    useArchivedSessions: () => ({ data: [] }),
    useGithubRepos: () => ({ data: [], isLoading: false, isError: false }),
    useCreateSession: () => ({ mutate: vi.fn(), isPending: false }),
    useRenameSession: () => ({ mutate: vi.fn(), isPending: false }),
    usePermanentDeleteSession: () => ({ mutate: (_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.(), isPending: false }),
    useSendPrompt: () => ({ mutate: vi.fn() }),
    useCloseSession: () => ({ mutate: (_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.(), isPending: false }),
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
})
