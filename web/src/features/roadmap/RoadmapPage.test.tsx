// What the routed page shows for each address. The three detail levels are
// tested with their own components; this file is only about which one the page
// picks — and that an address the map does not contain says so.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { MAP } from './__fixtures__/map'
import { RoadmapPage } from './RoadmapPage'
import { CAPABILITY_ROUTE, PRODUCT_ROUTE, ROADMAP_PATH } from './roadmapNavState'

const map = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false, isError: false }))

vi.mock('@/api/hooks', () => ({
  useRoadmapMap: () => map,
  useQueue: () => ({ data: { entries: [], skipped: 0, generatedAt: 'now' }, isLoading: false }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

function mount(path: string, over: Partial<typeof map> = {}) {
  Object.assign(map, { data: MAP, isLoading: false, isError: false }, over)
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[ROADMAP_PATH, PRODUCT_ROUTE, CAPABILITY_ROUTE].map((p) => (
          <Route key={p} path={p} element={<RoadmapPage />} />
        ))}
      </Routes>
    </MemoryRouter>,
  )
}

describe('RoadmapPage', () => {
  it('shows the portfolio when nothing is open', () => {
    mount('/roadmap')
    expect(screen.getByRole('heading', { level: 1, name: 'Roadmap' })).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: 'Portfolio' })).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()
  })

  it('says Not found for an address the map does not contain, with a way back', () => {
    mount('/roadmap/nope')
    expect(screen.getByText('Not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to the roadmap/ })).toHaveAttribute('href', '/roadmap')
  })

  it('says Not found for a capability the product does not have', () => {
    mount('/roadmap/kindash/capabilities/nope')
    expect(screen.getByText('Not found')).toBeInTheDocument()
  })

  it('holds the Not found back while the map is still loading', () => {
    mount('/roadmap/nope', { data: undefined, isLoading: true })
    expect(screen.queryByText('Not found')).toBeNull()
  })

  it('blames the failed fetch, not the address, when the map could not load', () => {
    mount('/roadmap/kindash', { data: undefined, isLoading: false, isError: true })
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the roadmap.')
    // The product exists; the map does not. Saying "renamed" here would be a lie.
    expect(screen.queryByText('Not found')).toBeNull()
  })

  it('never leaves the content region empty when the map is missing', () => {
    mount('/roadmap', { data: undefined, isLoading: false, isError: true })
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('No roadmap to show')).toBeInTheDocument()
  })

  it('offers the empty-products copy when the map is simply absent', () => {
    mount('/roadmap', { data: undefined, isLoading: false })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText(/No products yet/)).toBeInTheDocument()
  })
})
