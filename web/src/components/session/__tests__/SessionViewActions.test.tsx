import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionView } from '../SessionView'
import * as hooks from '@/api/hooks'

const detach = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({ detach }),
}))

// Minimal runtime stub — SessionView only needs these fields for the header + body shells.
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

// SessionBody / SessionComposer are heavy; stub them so we test only the header wiring.
vi.mock('../SessionBody', () => ({ SessionBody: () => <div data-testid="body" /> }))
vi.mock('../SessionComposer', () => ({ SessionComposer: () => <div data-testid="composer" /> }))
vi.mock('../CommandResultOverlay', () => ({ CommandResultOverlay: () => null }))

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
}))

function successMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(hooks.useCloseSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(successMutation() as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
})

describe('SessionView header — archive/delete', () => {
  it('renders the in-session Archive control', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: /archive session my session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /more session actions/i })).toBeInTheDocument()
  })

  it('archiving detaches the socket and navigates back', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />)

    await user.click(screen.getByRole('button', { name: /archive session my session/i }))

    expect(detach).toHaveBeenCalledWith('s1')
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it('Escape closes the overflow menu without exiting the session', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />)

    // Open the overflow menu.
    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.getByRole('menuitem', { name: /delete permanently/i })).toBeInTheDocument()

    // Press Escape — should close the menu, NOT exit the session.
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('menuitem', { name: /delete permanently/i })).not.toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
    expect(detach).not.toHaveBeenCalled()
  })

  it('Escape closes the delete confirm dialog without exiting the session', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} />)

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
