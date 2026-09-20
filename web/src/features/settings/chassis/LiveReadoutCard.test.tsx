import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import * as hooks from '@/api/hooks'
import { queryResult, idleQueryResult } from '@/test/reactQuery'
import { CAPACITY } from '@/features/delivery/capacity/__fixtures__/capacity'
import { LiveReadoutCard } from './LiveReadoutCard'

vi.mock('@/api/hooks', () => ({ useCapacity: vi.fn() }))

function mount(highlight?: 'hosts' | 'accounts') {
  const router = createMemoryRouter([{ path: '/', element: <LiveReadoutCard highlight={highlight} /> }])
  return render(<RouterProvider router={router} />)
}

describe('LiveReadoutCard (spec 2026-09-20 D10)', () => {
  beforeEach(() => { vi.mocked(hooks.useCapacity).mockReturnValue(queryResult(CAPACITY) as never) })

  it('renders the five rows, the binding text and the link', () => {
    mount()
    const group = screen.getByRole('group', { name: 'Live readout' })
    expect(within(group).getAllByRole('listitem')).toHaveLength(5)
    expect(within(group).getByText('viberpit: at the fleet-wide product cap')).toBeInTheDocument()
    expect(within(group).getByRole('link', { name: 'Open Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })

  it('marks the highlighted row as current', () => {
    mount('hosts')
    const group = screen.getByRole('group', { name: 'Live readout' })
    const items = within(group).getAllByRole('listitem')
    expect(items[1]).toHaveAttribute('aria-current', 'true')
    expect(items[0]).not.toHaveAttribute('aria-current')
  })

  it('loading and error states', () => {
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult() as never)
    mount()
    expect(screen.getByText('Loading the readout…')).toBeInTheDocument()
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult({ isError: true, status: 'error', isPending: false } as never) as never)
    mount()
    expect(screen.getByText('The readout is unavailable.')).toBeInTheDocument()
  })
})
