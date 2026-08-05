// DesktopShell — the sidebar session list keeps its scroll position across
// selection changes. The whole shell subtree remounts whenever the focused
// session changes (SessionRuntimeProvider is keyed by session id), which resets
// the freshly-mounted scroll container to 0 — so clicking a session used to make
// the list visibly jump. DesktopShell itself stays mounted, so it carries the
// position across the remount and restores it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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
    usePermanentDeleteSession: () => ({ mutate: vi.fn(), isPending: false }),
    useSendPrompt: () => ({ mutate: vi.fn() }),
    useCloseSession: () => ({ mutate: vi.fn(), isPending: false }),
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
  localStorage.clear()
})

describe('DesktopShell — sidebar list scroll preservation', () => {
  it('keeps the list scroll position when a session is opened', async () => {
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    const list = screen.getByTestId('session-list-scroll')
    list.scrollTop = 240
    fireEvent.scroll(list)

    await user.click(screen.getByRole('button', { name: /open session first/i }))

    // The shell remounted (selection change) — the recreated container must be
    // restored to the recorded position, not reset to 0.
    expect(screen.getByTestId('session-list-scroll').scrollTop).toBe(240)
  })

  it('keeps the list scroll position when switching between sessions', async () => {
    const user = userEvent.setup()
    renderWithClient(<DesktopShell />)

    await user.click(screen.getByRole('button', { name: /open session first/i }))

    const list = screen.getByTestId('session-list-scroll')
    list.scrollTop = 120
    fireEvent.scroll(list)

    await user.click(screen.getByRole('button', { name: /open session second/i }))
    expect(screen.getByTestId('session-list-scroll').scrollTop).toBe(120)
  })
})
