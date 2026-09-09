import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { MAP } from '../__fixtures__/map'
import { PortfolioDetail } from './PortfolioDetail'

// Every hook the tree under test reaches: `RankedList` calls `useQueue`, and
// `NeedsYouList` mounts `IncidentQueue`, which calls the two incident hooks.
vi.mock('@/api/hooks', () => ({
  useQueue: () => ({
    data: {
      entries: [
        { ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, product: 'kindash', title: 'Top', url: 'https://x/1', score: 9.5 },
        { ref: 'steiner385/viberpit#2', repo: 'steiner385/viberpit', number: 2, product: 'viberpit', title: 'Second', url: 'https://x/2', score: 4 },
      ],
      skipped: 0,
      generatedAt: 'now',
    },
    isLoading: false,
  }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

function Here() {
  const loc = useLocation()
  return <div data-testid="here">{loc.pathname + loc.search}</div>
}

function mount(initial = '/roadmap') {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <PortfolioDetail map={MAP} />
      <Here />
    </MemoryRouter>,
  )
}

const here = () => screen.getByTestId('here').textContent

describe('PortfolioDetail', () => {
  it('shows stat tiles and the Needs-you tab by default', () => {
    mount()
    expect(screen.getByText('Products')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Needs you/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('No home yet')).toBeInTheDocument() // needs-you initiative
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /2 awaiting triage/ })).toHaveAttribute('href', '/triage?projectSlug=kindash')
  })

  it('counts the whole portfolio in the tiles', () => {
    mount()
    // one product in flight, none blocked, one initiative needing a human
    const tile = (label: string) => screen.getByText(label).closest('div')
    expect(tile('In flight')).toHaveTextContent('1')
    expect(tile('Blocked')).toHaveTextContent('0')
    expect(tile('Products')).toHaveTextContent('2')
  })

  it('Ranked tab lists the queue across products in score order', () => {
    mount('/roadmap?tab=ranked')
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('Top')
    expect(rows[1]).toHaveTextContent('Second')
    expect(screen.getByRole('link', { name: /#1 Top/ })).toHaveAttribute('href', 'https://x/1')
  })

  it('All initiatives tab lists every initiative and opens one', () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /All initiatives/ }))
    expect(here()).toBe('/roadmap?tab=initiatives')
    const open = screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })
    expect(screen.getByRole('button', { name: 'Open initiative No home yet' })).toBeInTheDocument()
    fireEvent.click(open)
    expect(here()).toBe('/initiatives/views')
  })
})
