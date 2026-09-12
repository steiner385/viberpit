// What the routed page shows for each address. The three detail levels are
// tested with their own components; this file is only about which one the page
// picks — and that an address the map does not contain says so.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import type { TriageProposal } from '@/api/triage'
import { MAP } from './__fixtures__/map'
import { clearInboxOrder, setInboxOrder } from './inbox/inboxOrderStore'
import { RoadmapPage } from './RoadmapPage'
import { CAPABILITY_ROUTE, PRODUCT_ROUTE, ROADMAP_PATH } from './roadmapNavState'

const map = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false, isError: false }))
const sync = vi.hoisted(() => ({
  mutate: vi.fn(), isPending: false, isError: false, error: null as unknown,
}))
// The Inbox scope's queue and its three decision mutations. The page finds the
// open proposal in this list, so it is also what "the next proposal" means.
const triage = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false, isError: false }))
const accept = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }))
const reject = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }))
// The scope each decision mutation was keyed to: the cache entry its optimistic
// removal patches has to be the one the list column's rows came from.
const acceptScope = vi.hoisted(() => vi.fn())

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
      featureCount: 3, workItemCount: 40, automationPolicy: null,
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
  // The capability Overview's edits (Edit, + Sub-capability, the links editor).
  useEditMapChanges: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false, error: null }),
  mapChangesErrorMessage: (_e: unknown, fallback: string) => fallback,
  // The product Overview's map-changes PR card (Rebase, Abandon).
  useAbandonMapChanges: () => ({ mutate: vi.fn(), isPending: false }),
  useRebaseMapChanges: () => ({ mutate: vi.fn(), isPending: false }),
  // The product Overview's Automation card.
  usePolicySchema: () => ({ data: [], isLoading: false }),
  useUpdateProductPolicy: () => ({ mutate: vi.fn(), isPending: false }),
  useProposeMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useClassify: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceLive: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useQueue: () => ({ data: { entries: [], skipped: 0, generatedAt: 'now' }, isLoading: false }),
  useLeaves: () => ({ data: [], isLoading: false, isError: false }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useTriage: () => triage,
  useAcceptProposal: (slug?: string) => { acceptScope(slug); return accept },
  useRejectProposal: () => reject,
  // The product Overview's hygiene notice — no data, so it renders nothing.
  useProjectHealth: () => ({ data: undefined, isLoading: false, isError: false }),
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

/** The address after a decision — the pane navigates itself onward. */
function Location() {
  const { pathname, search } = useLocation()
  return <div data-testid="location">{pathname}{search}</div>
}

function mount(path: string, over: Partial<typeof map> = {}) {
  Object.assign(map, { data: MAP, isLoading: false, isError: false }, over)
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[ROADMAP_PATH, PRODUCT_ROUTE, CAPABILITY_ROUTE].map((p) => (
          <Route key={p} path={p} element={<RoadmapPage />} />
        ))}
      </Routes>
      <Location />
    </MemoryRouter>,
  )
}

const proposal = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: 'f-tasks', proposedFeatureSlug: 'tasks',
  noFitSuggestion: null, reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null,
  confidenceLevel: 'high', status: 'pending', createdAt: '2026-09-08T10:00:00Z', ...over,
})

const QUEUE: TriageProposal[] = [
  proposal({ id: 'p1', title: 'Bulk edit' }),
  proposal({ id: 'p2', title: 'Export CSV' }),
]

beforeEach(() => {
  // The rendered order the list column publishes is module state shared with
  // this page, so a test that publishes one must not leak into the ones that
  // assert the no-column fallback.
  clearInboxOrder()
  Object.assign(sync, { mutate: vi.fn(), isPending: false, isError: false, error: null })
  Object.assign(triage, { data: QUEUE, isLoading: false, isError: false })
  Object.assign(accept, { mutate: vi.fn(), isPending: false })
  Object.assign(reject, { mutate: vi.fn(), isPending: false })
  acceptScope.mockClear()
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

// The Inbox scope: the same page, a different body. The queue is the list
// column's; this side is the editor that decides one proposal at a time.
describe('RoadmapPage — Inbox scope', () => {
  it('is the Inbox, with a welcome that counts what is waiting', () => {
    mount('/roadmap?scope=inbox')
    expect(screen.getByRole('heading', { level: 1, name: 'Inbox' })).toBeInTheDocument()
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect([...crumbs.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Roadmap', 'Inbox'])
    expect(screen.getByText('Pick a proposal')).toBeInTheDocument()
    expect(screen.getByText(/2 proposals/)).toBeInTheDocument()
    // The portfolio belongs to the Map scope; nothing is "open" here either way.
    expect(screen.queryByRole('tablist', { name: 'Portfolio' })).toBeNull()
  })

  it('says inbox zero when the queue is empty', () => {
    Object.assign(triage, { data: [] })
    mount('/roadmap?scope=inbox')
    expect(screen.getByText('Inbox zero')).toBeInTheDocument()
  })

  it('names the product in the crumbs when the scope is narrowed', () => {
    mount('/roadmap?scope=inbox&product=kindash')
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect([...crumbs.querySelectorAll('li')].map((li) => li.textContent))
      .toEqual(['Roadmap', 'Inbox', 'kindash'])
  })

  it('offers no product actions in this scope', () => {
    mount('/roadmap?scope=inbox')
    expect(screen.queryByRole('button', { name: '+ Product' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sync' })).toBeNull()
  })

  it('opens the proposal named in the URL, with its product capabilities to home it on', () => {
    mount('/roadmap?scope=inbox&proposal=p1')
    expect(screen.getByText('Bulk edit')).toBeInTheDocument()
    expect(screen.getByLabelText('reach')).toHaveValue(3)
    // The capability select is built from the proposal's OWN product, so a
    // ViberPit capability can never be offered as a home for a KinDash issue.
    const options = [...(screen.getByRole('combobox', { name: 'Capability' }) as HTMLSelectElement).options]
      .map((o) => o.textContent?.trim())
    expect(options).toContain('Tasks')
    expect(options).toContain('Task views')
    expect(options).not.toContain('Sessions')
  })

  it('moves to the next proposal after a decision', () => {
    mount('/roadmap?scope=inbox&proposal=p1')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(accept.mutate).toHaveBeenCalledWith({ id: 'p1' })
    expect(screen.getByTestId('location')).toHaveTextContent('/roadmap?scope=inbox&proposal=p2')
  })

  it('takes "next" from the order the column is RENDERING, not the order the server sent', () => {
    // The server hands the queue over in `created_at DESC`; the column renders
    // it grouped/sorted/searched and publishes what it actually listed. After
    // p1 the user sees p3, so that is where an accept goes.
    Object.assign(triage, {
      data: [proposal({ id: 'p1' }), proposal({ id: 'p2' }), proposal({ id: 'p3' })],
    })
    setInboxOrder(['p1', 'p3', 'p2'])
    mount('/roadmap?scope=inbox&proposal=p1')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/roadmap?scope=inbox&proposal=p3')
  })

  it('follows the column after a RUNTIME sort change, not the order it started with', () => {
    // The regression this store exists for: the page used to run its own
    // `useItemOrdering`, which seeds group-by/sort-by from localStorage once
    // and never re-reads. Change the sort in the column and the pane's copy was
    // stale, so "next" stopped matching the screen. Here the column publishes
    // p1 · p2 · p3, then re-publishes p1 · p3 · p2 the way changing the sort
    // control does — and the accept has to honour the SECOND one.
    Object.assign(triage, {
      data: [proposal({ id: 'p1' }), proposal({ id: 'p2' }), proposal({ id: 'p3' })],
    })
    setInboxOrder(['p1', 'p2', 'p3'])
    mount('/roadmap?scope=inbox&proposal=p1')
    act(() => setInboxOrder(['p1', 'p3', 'p2']))
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/roadmap?scope=inbox&proposal=p3')
  })

  it('falls back to the queue order when no column is rendering one', () => {
    // The phone layout unmounts the list while a proposal is open, so the store
    // is empty (`clearInboxOrder` in the column's unmount effect). The pane
    // still has to advance, and the server's order is the only one left.
    Object.assign(triage, {
      data: [proposal({ id: 'p1' }), proposal({ id: 'p2' }), proposal({ id: 'p3' })],
    })
    clearInboxOrder()
    mount('/roadmap?scope=inbox&proposal=p1')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/roadmap?scope=inbox&proposal=p2')
  })

  it('steps back to the previous proposal when the decided one was last', () => {
    mount('/roadmap?scope=inbox&product=kindash&proposal=p2')
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(reject.mutate).toHaveBeenCalledWith({ id: 'p2' })
    // The `?product=` scope the user was working in survives the move.
    expect(screen.getByTestId('location'))
      .toHaveTextContent('/roadmap?scope=inbox&product=kindash&proposal=p1')
  })

  it('returns to the inbox root when there is nothing left to decide', () => {
    Object.assign(triage, { data: [QUEUE[0]] })
    mount('/roadmap?scope=inbox&product=kindash&proposal=p1')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    // Exactly the root: no `&proposal=` left pointing at the row just decided.
    expect(screen.getByTestId('location').textContent).toBe('/roadmap?scope=inbox&product=kindash')
  })

  it('keys the decision to the scope the list observes, not the proposal\'s product', () => {
    // Every fixture proposal is a KinDash one, but an UNSCOPED inbox lists them
    // from `[...QK.triage, null]` — accepting against `kindash` would remove
    // the row from a cache entry nobody is reading, and it would sit there
    // until the next refetch.
    mount('/roadmap?scope=inbox&proposal=p1')
    expect(acceptScope).toHaveBeenCalledWith(undefined)
    expect(acceptScope).not.toHaveBeenCalledWith('kindash')
  })

  it('keys it to the product once the inbox is narrowed', () => {
    mount('/roadmap?scope=inbox&product=kindash&proposal=p1')
    expect(acceptScope).toHaveBeenCalledWith('kindash')
  })

  it('shows one body, not two, on a product route carrying ?scope=inbox', () => {
    // The scope decides which body the page is; a product in the path does not
    // get to render a second one underneath it.
    mount('/roadmap/kindash?scope=inbox')
    expect(screen.getByRole('heading', { level: 1, name: 'Inbox' })).toBeInTheDocument()
    expect(screen.getByText('Pick a proposal')).toBeInTheDocument()
    expect(screen.queryByRole('tablist', { name: 'KinDash' })).toBeNull()
    expect(screen.queryByText('Not found')).toBeNull()
  })

  it('opens the score fields focused for ?edit=1', () => {
    mount('/roadmap?scope=inbox&proposal=p1&edit=1')
    expect(screen.getByLabelText('reach')).toHaveFocus()
  })

  it('says so when the proposal is not in the queue any more', () => {
    mount('/roadmap?scope=inbox&proposal=gone')
    expect(screen.getByText(/Proposal not found/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to the inbox/ }))
      .toHaveAttribute('href', '/roadmap?scope=inbox')
  })

  it('holds that back while the queue is still loading', () => {
    Object.assign(triage, { data: undefined, isLoading: true })
    mount('/roadmap?scope=inbox&proposal=p1')
    expect(screen.queryByText(/Proposal not found/)).toBeNull()
  })

  it('blames the failed fetch rather than the address when the queue could not load', () => {
    Object.assign(triage, { data: undefined, isLoading: false, isError: true })
    mount('/roadmap?scope=inbox&proposal=p1')
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the inbox.')
    expect(screen.queryByText(/Proposal not found/)).toBeNull()
  })
})
