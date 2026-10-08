import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

const queueMove = vi.fn()
const queueCancel = vi.fn()
const startNow = vi.fn()
vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig()),
  useQueueMove: () => ({ mutate: queueMove }),
  useQueueCancel: () => ({ mutate: queueCancel }),
  useQueueStartNow: () => ({ mutate: startNow }),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))

import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import { QueuedSection } from './QueuedSection'

const qs = (id: string, pos: number) => ({
  id, name: `sess-${id}`, status: 'queued', queuePosition: pos,
  account: '', repo: 'owner/repo', branch: null, updatedAt: '',
}) as never

describe('QueuedSection', () => {
  it('renders rows sorted by position with a count header', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('b', 2), qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.getByText(/Queued \(2\)/)).toBeInTheDocument()
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('sess-a')
  })

  it('wires move/cancel controls', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1), qs('b', 2)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getAllByLabelText('Move up')[1])
    expect(queueMove).toHaveBeenCalledWith({ id: 'b', op: 'up' })
    fireEvent.click(screen.getAllByLabelText('Remove from queue')[0])
    expect(queueCancel).toHaveBeenCalledWith({ id: 'a' })
  })

  it('renders nothing when empty', () => {
    const { container } = render(<MemoryRouter><QueuedSection sessions={[]} onOpen={() => {}} /></MemoryRouter>)
    expect(container).toBeEmptyDOMElement()
  })

  it('says why sessions wait and links to Capacity (spec 2026-09-20 C6)', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.getByText(/Waiting for capacity\./)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })

  it('shows the stall reason under the session name', () => {
    const s = { ...(qs('a', 1) as object), queueStallReason: { code: 'capacity_full', message: 'Global cap reached (3/3)' } } as never
    render(<MemoryRouter><QueuedSection sessions={[s]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.getByText('Global cap reached (3/3)')).toBeInTheDocument()
  })

  it('shows no reason line without a stall reason', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.getByText(/Waiting for capacity\./)).toBeInTheDocument()
    expect(screen.queryByText(/Global cap/)).toBeNull()
  })

  it('Start now calls the mutation with the session id', () => {
    const s = { ...(qs('s1', 1) as object), name: 'Alpha' } as never
    render(<MemoryRouter><QueuedSection sessions={[s]} onOpen={() => {}} /></MemoryRouter>)
    const btn = screen.getByRole('button', { name: 'Start Alpha now, over capacity limits' })
    expect(btn).toHaveTextContent('Start now')
    fireEvent.click(btn)
    expect(startNow).toHaveBeenCalledWith({ id: 's1' }, expect.objectContaining({ onError: expect.any(Function) }))
  })

  it('hides Start now when the account is limited (it cannot override that)', () => {
    const s = { ...(qs('a', 1) as object), queueStallReason: { code: 'account_limited', message: 'x' } } as never
    render(<MemoryRouter><QueuedSection sessions={[s]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.queryByRole('button', { name: /now, over capacity limits/ })).toBeNull()
  })

  it('says so in a status line when every account is limited', () => {
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'all_accounts_limited' } })))
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    expect(screen.getByRole('status')).toHaveTextContent('Account limit — starts automatically when quota frees')
  })

  it('shows the re-auth copy for accounts_need_reauth', () => {
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'accounts_need_reauth', message: 'x' } })))
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    expect(screen.getByRole('status')).toHaveTextContent(/re-authenticate/)
  })

  it("surfaces the server's message for start_failed", () => {
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'start_failed', message: 'boom' } })))
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    expect(screen.getByRole('status')).toHaveTextContent('boom')
  })

  it('toasts start_failed so it survives the ended row unmounting', () => {
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'start_failed', message: 'worktree exploded' } })))
    const { rerender } = render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    // The server ended the session; the refetch drops it from the queue.
    rerender(<MemoryRouter><QueuedSection sessions={[]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.queryByRole('status')).toBeNull()
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('worktree exploded'))
  })

  it('does not toast a still-queued refusal', () => {
    vi.mocked(toast.error).mockClear()
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'all_accounts_limited' } })))
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('drops a row error once its stall reason changes', () => {
    startNow.mockImplementationOnce((_v, opts) =>
      opts.onError(new ApiRequestError(409, { error: { code: 'start_failed', message: 'boom' } })))
    const a = { ...(qs('a', 1) as object), queueStallReason: { code: 'capacity_full', message: 'm' } }
    const { rerender } = render(<MemoryRouter><QueuedSection sessions={[a as never]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /now, over capacity limits/ }))
    expect(screen.getByRole('status')).toBeInTheDocument()
    const b = { ...a, queueStallReason: { code: 'account_limited', message: 'm' } }
    rerender(<MemoryRouter><QueuedSection sessions={[b as never]} onOpen={() => {}} /></MemoryRouter>)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('collapsing the section hides the wait explanation with the list', () => {
    render(<MemoryRouter><QueuedSection sessions={[qs('a', 1)]} onOpen={() => {}} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: /Queued \(1\)/ }))
    expect(screen.queryByText(/Waiting for capacity/)).toBeNull()
    expect(screen.queryByRole('link', { name: 'See capacity' })).toBeNull()
  })
})
