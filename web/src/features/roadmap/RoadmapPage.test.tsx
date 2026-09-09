// What the routed page shows for each address. The three detail levels are
// tested with their own components; this file is only about which one the page
// picks — and that an address the map does not contain says so.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import { MAP } from './__fixtures__/map'
import { RoadmapPage } from './RoadmapPage'
import { CAPABILITY_ROUTE, PRODUCT_ROUTE, ROADMAP_PATH } from './roadmapNavState'

const map = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false, isError: false }))
const sync = vi.hoisted(() => ({
  mutate: vi.fn(), isPending: false, isError: false, error: null as unknown,
}))

// Beyond the page's own `useRoadmapMap`: the detail levels it renders reach for
// the project, the queue, the incidents and the onboarding view.
vi.mock('@/api/hooks', () => ({
  useRoadmapMap: () => map,
  useSyncProject: () => sync,
  useCreateInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
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
  useLeaves: () => ({ data: [], isLoading: false, isError: false }),
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

beforeEach(() => {
  Object.assign(sync, { mutate: vi.fn(), isPending: false, isError: false, error: null })
})

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

  it('shows the capability detail under a breadcrumb of every ancestor', () => {
    mount('/roadmap/kindash/capabilities/task-views')
    expect(screen.getByRole('heading', { level: 1, name: 'Task views' })).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: 'Task views' })).toBeInTheDocument()
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' })
    // Roadmap › KinDash › Tasks › Task views — every ancestor is walkable back
    // up one level at a time, and the open capability is the current page.
    expect([...crumbs.querySelectorAll('li')].map((li) => li.textContent))
      .toEqual(['Roadmap', 'KinDash', 'Tasks', 'Task views'])
    expect(within(crumbs).getByRole('link', { name: 'Tasks' }))
      .toHaveAttribute('href', '/roadmap/kindash/capabilities/tasks')
    expect(within(crumbs).queryByRole('link', { name: 'Task views' })).toBeNull()
  })

  it('creates an initiative pre-homed on the open capability', () => {
    mount('/roadmap/kindash/capabilities/task-views')
    fireEvent.click(screen.getByRole('button', { name: /Initiative here/ }))
    expect(screen.getByLabelText(/Primary capability/)).toHaveValue('f-task-views')
  })

  it('offers no product actions when nothing (or nothing real) is open', () => {
    mount('/roadmap')
    expect(screen.queryByRole('button', { name: 'Sync' })).toBeNull()
  })

  it('offers + Product on the portfolio and opens the register dialog', () => {
    // The only way to register a product: the Products page this section
    // replaced took its new-product form with it.
    mount('/roadmap')
    fireEvent.click(screen.getByRole('button', { name: '+ Product' }))
    expect(screen.getByRole('dialog', { name: 'New product' })).toBeInTheDocument()
  })

  it('keeps + Product off a product page — it is the portfolio\'s action', () => {
    mount('/roadmap/kindash')
    expect(screen.queryByRole('button', { name: '+ Product' })).toBeNull()
  })

  it('offers the same action from the empty state, when there is no map at all', () => {
    mount('/roadmap', { data: undefined, isLoading: false })
    expect(screen.getByText(/No products yet/)).toBeInTheDocument()
    // Header action and empty-state action: both open the same dialog.
    const actions = screen.getAllByRole('button', { name: '+ Product' })
    expect(actions.length).toBe(2)
    fireEvent.click(actions[1]!)
    expect(screen.getByRole('dialog', { name: 'New product' })).toBeInTheDocument()
  })

  it('says so when a sync could not even be requested', () => {
    // `useSyncProject` suppresses the global error toast, so a page that never
    // renders `sync.error` reports a refusal as nothing at all.
    Object.assign(sync, {
      isError: true,
      error: new ApiRequestError(409, { detail: { message: 'Backfill already running' } }),
    })
    mount('/roadmap/kindash')
    expect(screen.getByRole('alert')).toHaveTextContent('Backfill already running')
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
