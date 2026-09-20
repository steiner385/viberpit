import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

const queueMove = vi.fn()
const queueCancel = vi.fn()
vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig()),
  useQueueMove: () => ({ mutate: queueMove }),
  useQueueCancel: () => ({ mutate: queueCancel }),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

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
    expect(screen.getByText(/Waiting for a free slot under the global cap/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
})
