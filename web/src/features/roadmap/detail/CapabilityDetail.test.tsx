// One capability's detail: what each tab answers, that every tile and the tab
// badge beside it count the same population, and that the issue list puts open
// work above closed history.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Leaf } from '@/api/roadmap'
import { ApiRequestError } from '@/api/client'
import { MAP, cap, ini, product } from '../__fixtures__/map'
import { findCapability } from '../lookup'
import { CapabilityDetail } from './CapabilityDetail'
import { ViewportProvider } from '@/shell/useViewport'

// The leaves query is per-test state: loading, error and empty are all states
// this panel has to render.
const leavesQuery = vi.hoisted(() => ({
  data: undefined as unknown,
  isLoading: false,
  isError: false,
}))

// The remove-dependency mutation is per-row, per-test state: whether it is
// pending or refused, and with what error.
const removeMutate = vi.hoisted(() => vi.fn())
const removeState = vi.hoisted(() => ({
  isPending: false,
  isError: false,
  error: null as unknown,
}))

// Every hook the tree under test reaches: `useLeaves` and `useRemoveDependency`
// in `LeafList`, and the two incident hooks in `IncidentQueue` (mounted by
// `NeedsYouList`).
vi.mock('@/api/hooks', () => ({
  useLeaves: () => leavesQuery,
  useRemoveDependency: () => ({ mutate: removeMutate, ...removeState }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

const leaf = (over: Partial<Leaf> & { id: string; number: number }): Leaf => ({
  ref: `cairnea/KinDash#${over.number}`, repo: 'cairnea/KinDash', kind: 'issue', title: null,
  url: `https://x/${over.number}`, state: 'open', triageState: 'classified', score: null, reach: null,
  impact: null, effort: null, confidence: null, tier: 'idle', blockedBy: [], parentRef: null,
  featureId: null, sessionStatus: null, ghCreatedAt: null, ...over,
})

const LEAVES: Leaf[] = [
  leaf({ id: 'w1', number: 7, title: 'Bulk edit', score: 6.5, tier: 'in-flight', sessionStatus: 'in_progress' }),
  leaf({ id: 'w2', number: 9, title: 'Blocked one', tier: 'blocked', blockedBy: ['cairnea/KinDash#7'] }),
  leaf({ id: 'w3', number: 3, title: 'Old', state: 'closed', tier: 'live' }),
  leaf({ id: 'w4', number: 11, title: 'No URL', url: null }),
]

// The shipped fixture: `tasks` ▸ `task-views`, whose only initiative is primary
// on the CHILD — the parent's own initiative list is empty.
const kindash = MAP.products[0]!
const taskViews = findCapability(kindash, 'task-views')!

// A capability with numbers on it, for the tile/badge consistency checks.
const tasks = cap({
  slug: 'tasks', title: 'Tasks', tier: 'in-flight', state: 'delivering',
  summary: 'Everything a family does with a task.',
  docPath: 'docs/product/features/tasks.md', commitSha: 'abc123', prNumber: 12,
  counts: { openIssues: 7, inFlight: 5, blocked: 2, needsYou: 1 },
  links: [{ kind: 'powered_by', projectSlug: 'viberpit', featureSlug: 'sessions' }],
  children: [cap({ slug: 'task-views', title: 'Task views', parentSlug: 'tasks', counts: { openIssues: 4, inFlight: 1, blocked: 0, needsYou: 0 } })],
})

const busyProduct = product({
  slug: 'kindash', name: 'KinDash',
  counts: { inFlight: 2, blocked: 1, needsYou: 3, capabilities: 2, awaitingTriage: 4, needsHome: 2 },
  capabilities: [tasks],
  initiatives: [
    ini({ slug: 'views', title: 'Use-case-first task views', projectSlug: 'kindash', primaryCapability: 'tasks' }),
    ini({ slug: 'stuck', title: 'Stuck one', projectSlug: 'kindash', primaryCapability: 'tasks', tier: 'needs-you', reason: 'advance' }),
    ini({ slug: 'shipped', title: 'Already shipped', projectSlug: 'kindash', primaryCapability: 'tasks', tier: 'live' }),
    ini({ slug: 'elsewhere', title: 'Somewhere else', projectSlug: 'kindash', primaryCapability: 'billing' }),
  ],
})

function mount(
  over: Partial<Parameters<typeof CapabilityDetail>[0]> = {},
  path = '/roadmap/kindash/capabilities/tasks',
) {
  const props = { product: busyProduct, capability: tasks, onNewInitiative: () => {}, ...over }
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter initialEntries={[path]}><CapabilityDetail {...props} /></MemoryRouter>
    </ViewportProvider>,
  )
}

/** The stat tile for a label, matched on its `dt` — the tab strip repeats the
 *  same words, and a plain text match would find the tab instead. */
const tile = (label: string) =>
  screen.getAllByText(label).find((el) => el.tagName === 'DT')?.closest('div')

beforeEach(() => {
  Object.assign(leavesQuery, { data: LEAVES, isLoading: false, isError: false })
  removeMutate.mockClear()
  Object.assign(removeState, { isPending: false, isError: false, error: null })
})

describe('CapabilityDetail', () => {
  it('Overview: state, tier, PR, summary and the feature doc at its SHA', () => {
    mount({ primaryRepo: 'cairnea/KinDash' })
    expect(screen.getByText('Delivering')).toBeInTheDocument()
    expect(screen.getByText('Everything a family does with a task.')).toBeInTheDocument()
    expect(screen.getByText(/PR #12/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /feature doc/ }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/blob/abc123/docs/product/features/tasks.md')
    // Linked capabilities are how a capability points at the one that powers it.
    expect(screen.getByRole('link', { name: /viberpit\/sessions/ }))
      .toHaveAttribute('href', '/roadmap/viberpit/capabilities/sessions')
  })

  it('Overview: no doc link without a repo to resolve it against', () => {
    mount()
    expect(screen.queryByRole('link', { name: /feature doc/ })).toBeNull()
    // Nothing failed — the link is simply not resolvable yet, so say nothing.
    expect(screen.queryByText(/Doc link unavailable/)).toBeNull()
  })

  it('Overview: says why the doc link is missing when the product fetch failed', () => {
    mount({ projectError: true })
    expect(screen.queryByRole('link', { name: /feature doc/ })).toBeNull()
    expect(screen.getByText(/Doc link unavailable — could not load the product/)).toBeInTheDocument()
  })

  it('Overview: stays quiet about a doc the capability does not have', () => {
    mount({ capability: cap({ slug: 'tasks', title: 'Tasks' }), projectError: true })
    expect(screen.queryByText(/Doc link unavailable/)).toBeNull()
  })

  it('Overview: a sub-capability points back at its parent', () => {
    mount({ product: kindash, capability: taskViews }, '/roadmap/kindash/capabilities/task-views')
    // Named the way the tree names it, not by its slug.
    expect(screen.getByRole('link', { name: 'Tasks' })).toHaveAttribute('href', '/roadmap/kindash/capabilities/tasks')
  })

  it('starts a new initiative here', () => {
    const onNewInitiative = vi.fn()
    mount({ onNewInitiative })
    fireEvent.click(screen.getByRole('button', { name: /Initiative here/ }))
    expect(onNewInitiative).toHaveBeenCalled()
  })

  it('every tile counts what the tab beside it lists', () => {
    mount()
    // Open issues: the 7 in `counts` is a rollup that includes the child's 4,
    // but the Issues panel lists direct children only — so both the tile and the
    // badge say 3, the number the panel can actually show.
    expect(tile('Open issues')).toHaveTextContent('3')
    expect(screen.getByRole('tab', { name: 'Issues (3)' })).toBeInTheDocument()
    // In flight: the INITIATIVES the tab lists (not `counts.inFlight`, which
    // also rolls up issue tiers), so the two numbers cannot disagree.
    expect(tile('In flight')).toHaveTextContent('2')
    expect(screen.getByRole('tab', { name: 'In flight (2)' })).toBeInTheDocument()
    expect(tile('Blocked')).toHaveTextContent('2')
    expect(tile('Needs you')).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Sub-capabilities (1)' })).toBeInTheDocument()
  })

  it('Sub-capabilities: each child links to its own address', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=children')
    expect(screen.getByRole('link', { name: 'Task views' }))
      .toHaveAttribute('href', '/roadmap/kindash/capabilities/task-views')
    expect(screen.getByText(/4 open/)).toBeInTheDocument()
  })

  it('Sub-capabilities: says so when it is a leaf capability', () => {
    mount({ product: kindash, capability: findCapability(kindash, 'billing')! }, '/roadmap/kindash/capabilities/billing?tab=children')
    expect(screen.getByText('No sub-capabilities')).toBeInTheDocument()
  })

  it('In flight: the initiatives on this capability, moving or stuck', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=in-flight')
    expect(screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open initiative Stuck one' })).toBeInTheDocument()
    // Shipped is history; the other capability's initiative is not ours at all.
    expect(screen.queryByText('Already shipped')).toBeNull()
    expect(screen.queryByText('Somewhere else')).toBeNull()
  })

  it('Issues: open work first, with its blockers, score and live session', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    // Excludes the "Blocked by" sub-list's own <li> refs, which share the
    // listitem role but are not leaf rows.
    const items = screen.getAllByRole('listitem').filter((el) => el.closest('[aria-label="Blocked by"]') === null)
    expect(items[0]).toHaveTextContent('Bulk edit')
    expect(items[0]).toHaveTextContent('session')
    expect(items[0]).toHaveTextContent('6.5')
    expect(screen.getByRole('link', { name: /#7 Bulk edit/ })).toHaveAttribute('href', 'https://x/7')
    expect(screen.getByLabelText('1 blocker')).toBeInTheDocument()
    // Closed leaves are kept, but below a heading that says what they are.
    expect(screen.getByRole('heading', { name: /Open · 3/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Closed · 1/ })).toBeInTheDocument()
    expect(items[3]).toHaveTextContent('Old')
  })

  it('Issues: a blocked leaf names each blocker and offers to remove it', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    const list = screen.getByRole('list', { name: 'Blocked by' })
    // Same repo as the leaf → short form; the link goes to the blocker on GitHub.
    expect(within(list).getByRole('link', { name: '#7' }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/issues/7')
    fireEvent.click(screen.getByRole('button', { name: 'Remove blocker cairnea/KinDash#7' }))
    expect(removeMutate).toHaveBeenCalledWith({ workItemId: 'w2', blocker: 'cairnea/KinDash#7' })
  })

  it('Issues: a refused removal says so on that row only', () => {
    Object.assign(removeState, {
      isError: true,
      error: new ApiRequestError(502, { detail: { error: 'dep_remove_failed', step: 'node_id', message: 'o/r#7 has no GitHub node id yet; wait for the next sync' } }),
    })
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    expect(screen.getByText('o/r#7 has no GitHub node id yet; wait for the next sync')).toBeInTheDocument()
  })

  it('Issues: a leaf with no URL is text, not a dead link', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    expect(screen.getByText(/#11 No URL/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /No URL/ })).toBeNull()
  })

  it('Issues: says so when the list could not be loaded', () => {
    Object.assign(leavesQuery, { data: undefined, isLoading: false, isError: true })
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the issues')
  })

  it('Issues: says so when there are none', () => {
    Object.assign(leavesQuery, { data: [], isLoading: false, isError: false })
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    expect(screen.getByText('No issues here yet')).toBeInTheDocument()
  })

  it('Needs you: this capability only — not the product’s proposals', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=needs-you')
    expect(screen.getByText('Stuck one')).toBeInTheDocument()
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    // The product's awaiting-triage decisions belong to the product level; a
    // capability that scoped them in would double-count them on every page.
    expect(screen.queryByText(/awaiting triage/)).toBeNull()
  })
})
