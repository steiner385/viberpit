// One product's detail: what each tab answers, and that its Needs-you number is
// the same number the portfolio shows for it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from '../__fixtures__/map'
import { needsYouCount } from './needsYou'
import { ProductDetail } from './ProductDetail'
import { ViewportProvider } from '@/shell/useViewport'

// The project query is per-test state: a failed fetch is one of the states this
// component has to render, and the map still holds the product either way.
const projectQuery = vi.hoisted(() => ({
  data: undefined as unknown,
  isLoading: false,
  isError: false,
}))
const onboardingQuery = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false }))

// Every hook the tree under test reaches: `useProject` here, `useQueue` in
// `RankedList`, the two incident hooks in `IncidentQueue` (mounted by
// `NeedsYouList`), and the onboarding hooks in `OnboardingWizard`.
vi.mock('@/api/hooks', () => ({
  useProject: () => projectQuery,
  useOnboarding: () => onboardingQuery,
  useProposeMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useClassify: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceMap: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAdvanceLive: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useQueue: () => ({
    data: {
      entries: [
        { ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, product: 'kindash', title: 'Top', url: 'https://x/1', score: 9.5 },
        { ref: 'steiner385/viberpit#2', repo: 'steiner385/viberpit', number: 2, product: 'viberpit', title: 'Other', url: 'https://x/2', score: 4 },
      ],
      skipped: 0,
      generatedAt: 'now',
    },
    isLoading: false,
  }),
  useIncidents: () => ({
    data: [{
      id: 'i1', repo: 'cairnea/KinDash', status: 'open', metric: 'ci_fail', summary: 'CI failing',
      key: null, sigma: 3, occurrences: 1, value: 1, baselineMean: 0, baselineStddev: 0, baselineN: 5,
      observedAt: null, rule: 'we1', dismissedReason: null, scheduledFor: null, initiativeSlug: null,
      sessionId: null, evidence: [], source: null, side: null, tier: null, response: null,
      createdAt: null, updatedAt: null,
    }],
    isError: false,
  }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRoadmapMap: () => ({ data: MAP }),
  useCreateEdge: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteEdge: () => ({ mutate: vi.fn(), isPending: false }),
  // The Overview's Automation card.
  usePolicySchema: () => ({ data: [], isLoading: false }),
  useProductImplementationPolicy: () => ({ data: undefined, isPending: false, isError: false }),
  useProductPolicyCoverage: () => ({ data: undefined, isPending: false, isError: false }),
  useUpdateProductPolicy: () => ({ mutate: vi.fn(), isPending: false }),
  useSdlcGates: () => ({ data: undefined, isPending: false, isError: false }),
  // `+ Capability` opens NewCapabilityDialog, which edits the map-changes PR.
  useEditMapChanges: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false, error: null }),
  mapChangesErrorMessage: (_e: unknown, fallback: string) => fallback,
  // MapChangesCard's Rebase/Abandon — no product in this file's fixture has an
  // open pointer by default, so these are exercised by MapChangesCard's own tests.
  useAbandonMapChanges: () => ({ mutate: vi.fn(), isPending: false }),
  useRebaseMapChanges: () => ({ mutate: vi.fn(), isPending: false }),
  // The Overview's hygiene notice — no data, so `RoadmapHealthCard` renders
  // nothing and stays out of this file's assertions.
  useProjectHealth: () => ({ data: undefined, isLoading: false, isError: false }),
}))

vi.mock('@/api/pm', async (orig) => ({
  ...(await orig<typeof import('@/api/pm')>()),
  useProjectDeps: () => ({
    data: {
      project: null,
      edges: [],
      drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
    },
    isLoading: false,
    isError: false,
  }),
}))

const PROJECT = {
  id: 'p', slug: 'kindash', name: 'KinDash', description: 'Family dashboard', status: null,
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos: [{ id: 'r1', repo: 'cairnea/KinDash', isPrimary: true, backfillDone: true, lastSyncedAt: null }],
  edges: [], featureCount: 3, workItemCount: 40, automationPolicy: null, automationPolicyUpdatedAt: null,
}

const ONBOARDING = {
  state: 'live',
  action: { type: 'live' },
  counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 3, openUnclassifiedCount: 0, pendingProposalCount: 2 },
  mapPr: null,
}

const product = MAP.products[0]!

const mount = (initial = '/roadmap/kindash') =>
  render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter initialEntries={[initial]}>
        <ProductDetail product={product} />
      </MemoryRouter>
    </ViewportProvider>,
  )

/** The stat tile for a label, matched on its `dt` — the tab strip repeats the
 *  same words, and a plain text match would find the tab instead. */
const tile = (label: string) =>
  screen.getAllByText(label).find((el) => el.closest('dt'))?.closest('div')

beforeEach(() => {
  Object.assign(projectQuery, { data: PROJECT, isLoading: false, isError: false })
  Object.assign(onboardingQuery, { data: ONBOARDING, isLoading: false })
})

describe('ProductDetail', () => {
  it('Overview: tiles, description, repos, in-flight initiatives with tracks', () => {
    mount()
    expect(screen.getByText('Family dashboard')).toBeInTheDocument()
    expect(screen.getByText(/cairnea\/KinDash/)).toBeInTheDocument()
    expect(screen.getByText(/40 issues mirrored/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Use-case-first task views' })
    expect(link).toHaveAttribute('href', '/initiatives/views')
    // The stage track travels with the initiative it belongs to.
    expect(screen.getAllByLabelText(/Build: in progress/).length).toBeGreaterThan(0)
    expect(tile('Capabilities')).toHaveTextContent('3')
  })

  it('Overview: shows the map-changes PR card when the product has an open pointer', () => {
    const openProduct = {
      ...product,
      mapChanges: {
        branch: 'ccswitch/map-changes/kindash', prNumber: 12,
        prUrl: 'https://github.com/steiner385/viberpit/pull/12', status: 'open' as const,
        openedAt: '2026-09-11T00:00:00Z', headSha: 'abc123', behind: false, conflicting: false,
      },
    }
    render(
      <ViewportProvider kiosk={null}>
        <MemoryRouter initialEntries={['/roadmap/kindash']}>
          <ProductDetail product={openProduct} />
        </MemoryRouter>
      </ViewportProvider>,
    )
    expect(screen.getByRole('link', { name: 'PR #12' })).toBeInTheDocument()
  })

  it('Overview: adds a capability from the product itself, not from the page header', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: '+ Capability' }))
    expect(screen.getByRole('dialog', { name: 'New capability' })).toBeInTheDocument()
  })

  it('counts In flight as the initiatives the In-flight tab lists', () => {
    mount()
    // The tile and the badge beside it must not be two different numbers: both
    // are the moving-or-stuck initiatives (`counts.inFlight` also rolls up issue
    // tiers, which belong to the capability level). `views`, `orphan`, and the
    // `audit` program's `audit-01` phase — the `audit` program itself is
    // excluded (leaf-only, `inFlight.ts`), so it is 3, not 4.
    expect(tile('In flight')).toHaveTextContent('3')
    expect(screen.getByRole('tab', { name: 'In flight (3)' })).toBeInTheDocument()
  })

  it('counts Needs you exactly as the portfolio counts it for this product', () => {
    expect(needsYouCount([product])).toBe(2) // the needs-you initiative + awaiting triage
    mount()
    expect(tile('Needs you')).toHaveTextContent('2')
    expect(screen.getByRole('tab', { name: 'Needs you (2, needs attention)' })).toBeInTheDocument()
  })

  it('Needs you: product-scoped initiatives and incidents', () => {
    mount('/roadmap/kindash?tab=needs-you')
    expect(screen.getByText('No home yet')).toBeInTheDocument()
    // The incident row is named by its summary (the visible line is the metric).
    expect(screen.getByLabelText('CI failing')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Incidents' })).toBeInTheDocument()
  })

  it('shows no incidents at all until the repo list has arrived', () => {
    // `repos === undefined` means "every repo" to IncidentQueue, so rendering
    // the queue before the project resolves put OTHER products' incidents on
    // this product's page.
    Object.assign(projectQuery, { data: undefined, isLoading: true, isError: false })
    mount('/roadmap/kindash?tab=needs-you')
    expect(screen.queryByLabelText('CI failing')).toBeNull()
    expect(screen.queryByRole('region', { name: 'Incidents' })).toBeNull()
    // A placeholder stands in, rather than a panel that is simply blank.
    expect(screen.getByRole('tabpanel').querySelector('[aria-hidden="true"]')).not.toBeNull()
  })

  it('shows the Banner, not an unscoped queue, when the product fetch failed', () => {
    Object.assign(projectQuery, { data: undefined, isLoading: false, isError: true })
    mount('/roadmap/kindash?tab=needs-you')
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this product.')
    // The empty scope is a real scope: the rows the MAP carries still render.
    expect(screen.queryByLabelText('CI failing')).toBeNull()
    expect(screen.getByText('No home yet')).toBeInTheDocument()
  })

  it('In flight: every initiative that is moving or stuck', () => {
    mount('/roadmap/kindash?tab=in-flight')
    expect(screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open initiative No home yet' })).toBeInTheDocument()
  })

  it('a phase in flight names the program it belongs to, in the overview and the In-flight tab', () => {
    // `inFlightInitiatives` is leaf-only, so `audit-01` (parented under the
    // `audit` program) has no program row of its own to nest under at product
    // level — the badge is how it still says which program it is part of.
    mount()
    expect(screen.getByText('01 Restore')).toBeInTheDocument()
    expect(screen.getByTitle('Part of UI audit')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /In flight/ }))
    expect(screen.getByTitle('Part of UI audit')).toBeInTheDocument()
  })

  it('Ranked: only this product', () => {
    mount('/roadmap/kindash?tab=ranked')
    expect(screen.getByText(/Top/)).toBeInTheDocument()
    expect(screen.queryByText(/Other/)).toBeNull()
  })

  it('Dependencies tab renders the panel', () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }))
    expect(screen.getByText('Between products')).toBeInTheDocument()
    expect(screen.getByRole('tabpanel')).toHaveTextContent('No mirrored dependencies.')
  })

  it('says so when the product details could not be loaded, and still shows the map', () => {
    Object.assign(projectQuery, { data: undefined, isLoading: false, isError: true })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this product.')
    // The tree came from the map, so it survives a failed project fetch.
    expect(screen.getByRole('link', { name: 'Use-case-first task views' })).toBeInTheDocument()
  })

  it('shows the onboarding wizard while the product is not live yet', () => {
    Object.assign(onboardingQuery, {
      data: { ...ONBOARDING, state: 'map_proposed', action: { type: 'propose_map' } },
      isLoading: false,
    })
    mount()
    expect(screen.getByText('Capability map proposed')).toBeInTheDocument()
  })
})
