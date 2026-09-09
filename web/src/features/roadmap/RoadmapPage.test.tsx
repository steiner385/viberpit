// What the routed page shows for each address. The three detail levels are
// tested with their own components; this file is only about which one the page
// picks — and that an address the map does not contain says so.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { MAP } from './__fixtures__/map'
import { RoadmapPage } from './RoadmapPage'
import { CAPABILITY_ROUTE, PRODUCT_ROUTE, ROADMAP_PATH } from './roadmapNavState'

const map = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false, isError: false }))
const sync = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false }))

// Beyond the page's own `useRoadmapMap`: the detail levels it renders reach for
// the project, the queue, the incidents and the onboarding view.
vi.mock('@/api/hooks', () => ({
  useRoadmapMap: () => map,
  useSyncProject: () => sync,
  useCreateInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useProject: () => ({
    data: {
      id: 'p', slug: 'kindash', name: 'KinDash', description: 'Family dashboard', status: null,
      portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null, edges: [],
      featureCount: 3, workItemCount: 40,
      repos: [{ id: 'r1', repo: 'cairnea/KinDash', isPrimary: true, backfillDone: true, lastSyncedAt: null }],
    },
    isLoading: false,
    isError: false,
  }),
  useOnboarding: () => ({
    data: {
      state: 'live', action: { type: 'live' }, mapPr: null,
      counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 3, openUnclassifiedCount: 0, pendingProposalCount: 0 },
    },
    isLoading: false,
  }),
  useProposeMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useClassify: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceLive: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useQueue: () => ({ data: { entries: [], skipped: 0, generatedAt: 'now' }, isLoading: false }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

vi.mock('@/api/pm', async (orig) => ({
  ...(await orig<typeof import('@/api/pm')>()),
  useProjectDeps: () => ({
    data: {
      project: null, edges: [],
      drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
    },
    isLoading: false,
    isError: false,
  }),
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

  it('shows the product detail, its breadcrumb and its actions for a product', () => {
    mount('/roadmap/kindash')
    expect(screen.getByRole('heading', { level: 1, name: 'KinDash' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: 'KinDash' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sync' }))
    expect(sync.mutate).toHaveBeenCalledWith('kindash')
  })

  it('opens the new-initiative dialog from the header', () => {
    mount('/roadmap/kindash')
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '+ Initiative' }))
    expect(screen.getByRole('dialog', { name: 'New initiative' })).toBeInTheDocument()

    // Abandoning it and opening it again starts from a blank form.
    fireEvent.change(screen.getByLabelText(/Title/), { target: { value: 'Half a thought' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '+ Initiative' }))
    expect(screen.getByLabelText(/Title/)).toHaveValue('')
  })

  it('offers no product actions when nothing (or nothing real) is open', () => {
    mount('/roadmap')
    expect(screen.queryByRole('button', { name: 'Sync' })).toBeNull()
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
