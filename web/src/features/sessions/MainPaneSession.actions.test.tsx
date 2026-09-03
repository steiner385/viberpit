import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MainPaneSession } from './MainPaneSession'
import { SessionRuntimeProvider } from '@/components/session/SessionRuntimeProvider'
import * as hooks from '@/api/hooks'

/**
 * The session header's Archive / ⋯ actions.
 *
 * Was `SessionViewActions.test.tsx`. `SessionView` is deleted (Task 18 / Item 2)
 * and `MainPaneSession` is the one session pane; `canRename` moved with it,
 * because the phone list row has no rename button and the list is not on screen
 * beside an open session, so the overflow menu is still rename's only home
 * there. It also absorbs `DesktopSessionActions.test.tsx`'s single case ("renders
 * the in-session Archive + more-actions controls"), which was the same
 * assertion against the other shell.
 */

const detach = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({ detach }),
}))

// Minimal runtime stub — the pane only needs these fields for the header + body shells.
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    title: 'My Session',
    mode: 'interactive',
    hasOpenAction: false,
    onSetMode: vi.fn(),
    commandResult: null,
    commands: [],
    dismissCommandResult: vi.fn(),
    state: { transcript: [], turns: [], status: 'idle', model: null },
  }),
}))

// SessionBody is heavy; stub it so this file tests only the header wiring.
// (`SessionView` handed its actions DOWN into the body's meta strip via
// `actionsSlot`; `MainPaneSession` renders them in its own header row, so the
// stub takes no slot — the assertions below are unchanged either way, because
// they query by accessible name rather than by position.)
vi.mock('@/components/session/SessionBody', () => ({
  SessionBody: () => <div data-testid="body" />,
}))
vi.mock('@/components/session/CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  // Mobile-only Rename (added when rename was dropped from the mobile session
  // card — the detail view is its new home so the action isn't stranded).
  useRenameSession: vi.fn(),
  useSessionsViewing: vi.fn(),
  // useSessionNeighbors (added in mobile nav feature) calls useSessions internally.
  // Return an empty list so the chevrons stay hidden in these header-focused tests.
  useSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

function successMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}

function renameMutation() {
  return { mutate: vi.fn((_args: { id: string; name: string }, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(hooks.useCloseSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(renameMutation() as unknown as ReturnType<typeof hooks.useRenameSession>)
})

function renderPane({ onDetach, onClosed }: { onDetach: () => void; onClosed: () => void }) {
  return render(
    <SessionRuntimeProvider sessionId="s1">
      <MainPaneSession
        sessionId="s1"
        onClosed={onClosed}
        onDetach={onDetach}
        onOpenSession={vi.fn()}
      />
    </SessionRuntimeProvider>,
  )
}

describe('MainPaneSession header — archive/delete', () => {
  it('renders the in-session Archive control', () => {
    renderPane({ onDetach: vi.fn(), onClosed: vi.fn() })
    expect(screen.getByRole('button', { name: /archive session my session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more session actions/i })).toBeInTheDocument()
  })

  it('archiving detaches the socket and calls onDone (not onBack)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    const onDone = vi.fn()
    renderPane({ onDetach: onBack, onClosed: onDone })

    await user.click(screen.getByRole('button', { name: /archive session my session/i }))

    expect(detach).toHaveBeenCalledWith('s1')
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onBack).not.toHaveBeenCalled()
  })

  it('Escape closes the overflow menu without exiting the session', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane({ onDetach: onBack, onClosed: vi.fn() })

    // Open the overflow menu.
    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.getByRole('menuitem', { name: /delete permanently/i })).toBeInTheDocument()

    // Press Escape — should close the menu, NOT exit the session.
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('menuitem', { name: /delete permanently/i })).not.toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
    expect(detach).not.toHaveBeenCalled()
  })

  it('exposes Rename in the overflow menu (its only home on mobile)', async () => {
    const user = userEvent.setup()
    renderPane({ onDetach: vi.fn(), onClosed: vi.fn() })

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
  })

  it('clicking Rename opens the rename dialog (and does not exit the session)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane({ onDetach: onBack, onClosed: vi.fn() })

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))

    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
    expect(detach).not.toHaveBeenCalled()
  })

  it('submitting the rename calls the rename mutation and closes the dialog', async () => {
    const mutate = vi.fn((_args: { id: string; name: string }, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.())
    vi.mocked(hooks.useRenameSession).mockReturnValue({ mutate, isPending: false } as unknown as ReturnType<typeof hooks.useRenameSession>)

    const user = userEvent.setup()
    renderPane({ onDetach: vi.fn(), onClosed: vi.fn() })

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))

    const input = screen.getByLabelText(/session name/i)
    await user.clear(input)
    await user.type(input, 'Renamed session')
    await user.keyboard('{Enter}')

    expect(mutate).toHaveBeenCalledWith({ id: 's1', name: 'Renamed session' }, expect.anything())
    expect(screen.queryByRole('dialog', { name: /rename session/i })).not.toBeInTheDocument()
  })

  it('Escape closes the delete confirm dialog without exiting the session', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane({ onDetach: onBack, onClosed: vi.fn() })

    // Open the overflow menu, then click Delete permanently to open the confirm dialog.
    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    // Press Escape — should close the dialog, NOT exit the session.
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
    expect(detach).not.toHaveBeenCalled()
  })
})
