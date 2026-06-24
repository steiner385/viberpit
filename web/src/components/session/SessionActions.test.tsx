import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionActions } from './SessionActions'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useCloseSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
}))

// mutate(id, opts) helpers that synchronously invoke the success/error callbacks,
// so we can assert the archive-then-delete sequencing without async plumbing.
function successMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()), isPending: false }
}
function errorMutation() {
  return { mutate: vi.fn((_id: string, opts?: { onError?: (e: unknown) => void }) => opts?.onError?.(new Error('boom'))), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SessionActions', () => {
  it('Archive: clicking archives the session then calls onDone (no dialog)', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /archive session my session/i }))

    expect(close.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(perm.mutate).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('Delete: opens confirm, then archive-then-delete in order, then onDone', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))

    // Confirm dialog appears; onDone not called yet.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(close.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(perm.mutate).toHaveBeenCalledWith('s1', expect.anything())
    expect(close.mutate.mock.invocationCallOrder[0]).toBeLessThan(perm.mutate.mock.invocationCallOrder[0])
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('Delete: if archive fails, it does NOT permanent-delete and does NOT call onDone', async () => {
    const user = userEvent.setup()
    const close = errorMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(perm.mutate).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('Delete: Cancel closes the dialog without mutating', async () => {
    const user = userEvent.setup()
    const close = successMutation()
    const perm = successMutation()
    vi.mocked(hooks.useCloseSession).mockReturnValue(close as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(perm as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    const onDone = vi.fn()

    render(<SessionActions sessionId="s1" sessionName="My Session" onDone={onDone} />)

    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete permanently/i }))
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(close.mutate).not.toHaveBeenCalled()
    expect(perm.mutate).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })
})
