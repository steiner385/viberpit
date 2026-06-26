import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MainPaneSession } from '../DesktopShell'

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // SessionActions calls this unconditionally (rules of hooks); the desktop
  // header doesn't surface Rename, but the hook still runs.
  useRenameSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionsViewing: vi.fn(),
}))

// Provide a runtime via the context the pane reads from. DesktopShell imports both
// the provider and the hook from ./session/SessionRuntimeProvider, so mock that module
// (keep SessionRuntimeProvider as a pass-through so other importers don't break).
vi.mock('../session/SessionRuntimeProvider', () => ({
  SessionRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useSessionRuntimeContext: () => ({
    title: 'Desk Session',
    commandResult: null,
    commands: [],
    dismissCommandResult: vi.fn(),
  }),
}))
vi.mock('../session/SessionBody', () => ({ SessionBody: () => <div /> }))
vi.mock('../session/CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

beforeEach(() => vi.clearAllMocks())

describe('Desktop MainPaneSession header — archive/delete', () => {
  it('renders the in-session Archive + more-actions controls', () => {
    render(<MainPaneSession sessionId="s1" onClosed={vi.fn()} />)
    expect(screen.getByRole('button', { name: /archive session desk session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more session actions/i })).toBeInTheDocument()
  })
})
