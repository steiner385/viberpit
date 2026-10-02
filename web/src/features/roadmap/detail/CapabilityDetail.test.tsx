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
import { initiativeKey } from '../treeRows'
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

// The map-changes mutation every Overview edit goes through.
const editMapChanges = vi.hoisted(() => vi.fn())

// Every hook the tree under test reaches: `useLeaves` and `useRemoveDependency`
// in `LeafList`, and the two incident hooks in `IncidentQueue` (mounted by
// `NeedsYouList`).
vi.mock('@/api/hooks', () => ({
  useHolds: () => ({ data: undefined }),
  useLeaves: () => leavesQuery,
  useRemoveDependency: () => ({ mutate: removeMutate, ...removeState }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  // The capability edits: the Dependencies tab's links chip editor, plus the
  // Edit and + Sub-capability dialogs (the editor also reads the map for link targets).
  useEditMapChanges: () => ({ mutateAsync: editMapChanges, isPending: false, isError: false, error: null }),
  useRoadmapMap: () => ({ data: MAP }),
  mapChangesErrorMessage: (_e: unknown, fallback: string) => fallback,
}))

const leaf = (over: Partial<Leaf> & { id: string; number: number }): Leaf => ({
  ref: `cairnea/KinDash#${over.number}`, repo: 'cairnea/KinDash', kind: 'issue', title: null,
  url: `https://x/${over.number}`, state: 'open', triageState: 'classified', score: null, reach: null,
  impact: null, effort: null, confidence: null, tier: 'idle', blockedBy: [], parentRef: null,
  featureId: null, sessionStatus: null, ghCreatedAt: null, reason: null, bounces: 0,
  holdKind: null, holdReason: null, holdBy: null, holdSetAt: null, ...over,
})

const LEAVES: Leaf[] = [
  // `featureId` names a capability in `busyProduct`'s tree (`cap()` ids are
  // `f-<slug>`), so the row's move chip has a tree to render from.
  leaf({ id: 'w1', number: 7, title: 'Bulk edit', score: 6.5, tier: 'in-flight', sessionStatus: 'in_progress', featureId: 'f-tasks' }),
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

const OPEN_PR = {
  branch: 'pm/map-changes-kindash', prNumber: 31, prUrl: 'https://github.com/o/r/pull/31',
  status: 'open', openedAt: null, headSha: null, behind: false, conflicting: false,
}

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
      {/* Real mount point is inside PageContent, whose scroll div carries
          data-page-scroll — LeafList's virtualizer measures against it. */}
      <div data-page-scroll="">
        <MemoryRouter initialEntries={[path]}><CapabilityDetail {...props} /></MemoryRouter>
      </div>
    </ViewportProvider>,
  )
}

/** The stat tile for a label, matched on its `dt` — the tab strip repeats the
 *  same words, and a plain text match would find the tab instead. */
const tile = (label: string) =>
  screen.getAllByText(label).find((el) => el.closest('dt'))?.closest('div')

beforeEach(() => {
  Object.assign(leavesQuery, { data: LEAVES, isLoading: false, isError: false })
  removeMutate.mockClear()
  Object.assign(removeState, { isPending: false, isError: false, error: null })
})

describe('CapabilityDetail', () => {
  it('offers a next step from Overview and includes attention issues in Needs you', () => {
    const attentionCap = cap({ slug: 'auth', title: 'Authentication', tier: 'needs-you', state: 'live',
      counts: { openIssues: 1, needsYou: 1, blocked: 0, inFlight: 0 } })
    const attentionProduct = product({ slug: 'kindash', capabilities: [attentionCap] })
    leavesQuery.data = [leaf({ id: 'attention', number: 42, title: 'Restore login', tier: 'needs-you' })]
    mount({ product: attentionProduct, capability: attentionCap })
    expect(tile('Needs you')).toHaveTextContent('1')
    fireEvent.click(screen.getByRole('button', { name: 'Review what needs you' }))
    expect(screen.getByRole('tab', { name: /Needs you/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('#42 Restore login')).toBeInTheDocument()
    expect(screen.queryByText('Nothing needs you')).not.toBeInTheDocument()
  })

  it('opens the issue list directly from the Overview action', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Review issues' }))
    expect(screen.getByRole('tab', { name: /Issues/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('#7 Bulk edit')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Issues/ })).toHaveFocus()
  })

  it('Overview: state, tier, PR, summary and the feature doc at its SHA', () => {
    mount({ primaryRepo: 'cairnea/KinDash' })
    expect(screen.getByText('Delivering')).toBeInTheDocument()
    expect(screen.getByText('Everything a family does with a task.')).toBeInTheDocument()
    expect(screen.getByText(/PR #12/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /feature doc/ }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/blob/abc123/docs/product/features/tasks.md')
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

  it('Overview: edits the capability and adds one under it', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('dialog', { name: 'Edit capability' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: '+ Sub-capability' }))
    expect(screen.getByRole('dialog', { name: 'New capability' })).toBeInTheDocument()
  })

  it('Overview: says where a change is waiting, and mounts no editor (PAGE-TABS §5)', () => {
    // `prNumber` alone is history once the PR merged; the pending note needs the
    // product's map-changes pointer to still be open.
    mount({ product: { ...busyProduct, mapChanges: OPEN_PR } })
    expect(screen.getByRole('link', { name: 'PR #31' })).toHaveAttribute('href', 'https://github.com/o/r/pull/31')
    expect(screen.queryByRole('button', { name: /Remove link/ })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Links' })).toBeNull()
  })

  it('Dependencies: what it is linked to is an editor on its own tab', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=dependencies')
    expect(screen.getByRole('tab', { name: 'Dependencies (1)' })).toHaveAttribute('aria-selected', 'true')
    // Linked capabilities are how a capability points at the one that powers
    // it — named by the target's title, not its slug path.
    expect(screen.getByRole('link', { name: 'powered by · Sessions (ViberPit)' }))
      .toHaveAttribute('href', '/roadmap/viberpit/capabilities/sessions')
    expect(screen.getByRole('button', { name: 'Remove link: powered by · Sessions (ViberPit)' })).toBeInTheDocument()
  })

  it('Overview: no pending note while the product has no open map-changes PR', () => {
    mount()
    expect(screen.queryByText(/Changes pending in/)).toBeNull()
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
    expect(tile('Needs you')).toHaveTextContent('3') // one initiative and two blocked issues
    expect(screen.getByRole('tab', { name: 'Needs you (3, needs attention)' })).toBeInTheDocument()
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

  it('In flight: a program appears above its in-flight phase, but only the phase counts', () => {
    // `MAP`'s `tasks` capability carries the `audit` program and its
    // `audit-01` phase (both `primaryCapability: 'tasks'`) — the fixture this
    // task's nesting exercises (`__fixtures__/map.ts`).
    const capability = findCapability(kindash, 'tasks')!
    const { container } = mount({ product: kindash, capability }, '/roadmap/kindash/capabilities/tasks?tab=in-flight')
    const rowIds = [...container.querySelectorAll('[data-testid^="row-initiative:"]')]
      .map((el) => el.getAttribute('data-testid'))
    expect(rowIds).toEqual([`row-${initiativeKey('audit')}`, `row-${initiativeKey('audit-01')}`])
    // Leaf-only (`inFlight.ts`): the program is shown for context but never
    // counted — only its phase is in flight.
    expect(tile('In flight')).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: 'In flight (1)' })).toBeInTheDocument()
  })

  it('In flight: a program interleaves before its first phase, not ahead of a leaf that precedes it in map order', () => {
    // A leaf (`leaf-a`) that has no program at all comes BEFORE the program
    // (`prog`) in `product.initiatives` (map order). The old code prepended
    // every qualifying program ahead of the whole in-flight list, which would
    // have put `prog` above `leaf-a` even though `leaf-a` comes first on the
    // map; the fix keeps map order and only inserts `prog` right before its
    // own first phase.
    const workProduct = product({
      slug: 'work-product', name: 'Work',
      capabilities: [cap({ slug: 'work', title: 'Work' })],
      initiatives: [
        ini({ slug: 'leaf-a', title: 'Leaf A', projectSlug: 'work-product', primaryCapability: 'work', tier: 'in-flight' }),
        ini({ slug: 'prog', title: 'Program', projectSlug: 'work-product', primaryCapability: 'work', hasChildren: true }),
        ini({ slug: 'phase-a', title: 'Phase A', projectSlug: 'work-product', primaryCapability: 'work', parentInitiative: 'prog', tier: 'in-flight' }),
      ],
    })
    const { container } = mount(
      { product: workProduct, capability: findCapability(workProduct, 'work')! },
      '/roadmap/work-product/capabilities/work?tab=in-flight',
    )
    const rowIds = [...container.querySelectorAll('[data-testid^="row-initiative:"]')]
      .map((el) => el.getAttribute('data-testid'))
    expect(rowIds).toEqual([
      `row-${initiativeKey('leaf-a')}`,
      `row-${initiativeKey('prog')}`,
      `row-${initiativeKey('phase-a')}`,
    ])
  })

  it('Issues: open work first, with its blockers, score and live session', () => {
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    // Excludes the "Blocked by" sub-list's own <li> refs and the virtualizer's
    // section-header rows, which all share the listitem role but are not leaf rows.
    const items = screen.getAllByRole('listitem')
      .filter((el) => el.closest('[aria-label="Blocked by"]') === null && el.querySelector('h3') === null)
    expect(items[0]).toHaveTextContent('Blocked one')
    expect(items[1]).toHaveTextContent('Bulk edit')
    expect(items[1]).toHaveTextContent('session')
    expect(items[1]).toHaveTextContent('6.5')
    expect(within(items[1]).getByRole('button', { name: /#7 Bulk edit/ })).toBeEnabled()
    expect(within(items[1]).getByRole('button', { name: 'Review and decide' })).toBeEnabled()
    expect(within(items[1]).getByRole('link', { name: 'View on GitHub' })).toHaveAttribute('href', 'https://x/7')
    expect(screen.getByLabelText('1 blocker')).toBeInTheDocument()
    // Closed leaves are kept, but below a heading that says what they are.
    expect(screen.getByRole('heading', { name: /Open · 3/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Closed · 1/ })).toBeInTheDocument()
    expect(items[3]).toHaveTextContent('Old')
  })

  it('Issues: a row offers to move the issue, which needs the product tree passed down', () => {
    // Pins the `product` prop end-to-end: drop it at the LeafList mount and the
    // chip disappears (the flattened tree is empty), which nothing else notices.
    mount({}, '/roadmap/kindash/capabilities/tasks?tab=issues')
    expect(screen.getAllByRole('button', { name: /Move #\d+/ }).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Move #7 to another capability' })).toHaveTextContent('Tasks')
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

it('explains issue-only attention on Overview and opens its actions', () => {
  const capability = cap({ slug: 'reauth', title: 'Re-authentication', tier: 'needs-you', counts: { openIssues: 1, needsYou: 1, blocked: 0, inFlight: 0 } })
  mount({ product: product({ slug: 'p', capabilities: [capability] }), capability })
  expect(tile('Needs you')).toHaveTextContent('1')
  expect(screen.getByText(/1 item needs attention/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Review what needs you' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Needs you: 1' }))
  expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByText('Nothing needs you')).toBeNull()
})

it('routes descendant-only issue attention to the child and retains a blocked initiative', () => {
  const child = cap({ slug: 'child', title: 'Child', counts: { openIssues: 1, needsYou: 1, blocked: 0, inFlight: 0 } })
  const capability = cap({ slug: 'parent', children: [child], counts: { openIssues: 1, needsYou: 1, blocked: 1, inFlight: 0 } })
  const p = product({ slug: 'p', capabilities: [capability], initiatives: [
    ini({ slug: 'stalled', title: 'Stalled work', projectSlug: 'p', primaryCapability: 'parent', tier: 'blocked', reason: 'blocked' }),
    ini({ slug: 'other', title: 'Other work', projectSlug: 'p', primaryCapability: 'elsewhere', tier: 'blocked' }),
  ] })
  mount({ product: p, capability }, '/roadmap/p/capabilities/parent?tab=needs-you')
  expect(screen.getByRole('link', { name: 'Review issues in Child' })).toHaveAttribute('href', '/roadmap/p/capabilities/child?tab=needs-you')
  expect(screen.getByRole('link', { name: 'Clear the block' })).toHaveAttribute('href', '/initiatives/stalled')
  expect(screen.queryByText('Other work')).toBeNull()
  expect(tile('Needs you')).toHaveTextContent('2')
})

// ── the queue leads (2026-09-30 Roadmap critique, P1; docs/PAGE-TABS.md §4) ──
// Needs you was the SIXTH tab here — off-screen at 390px. Tiles lead with the
// queue on every level and the tabs follow the tile order after Overview.
describe('CapabilityDetail — the queue leads', () => {
  it('tiles run Needs you · Blocked · In flight · Open issues, and tabs follow after Overview', () => {
    const { container } = mount()
    const tiles = [...container.querySelectorAll('dl dt')].map((dt) => dt.textContent?.trim())
    expect(tiles).toEqual(['Needs you', 'Blocked', 'In flight', 'Open issues'])
    const tabs = screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label') ?? t.textContent ?? '')
    expect(tabs[0]).toMatch(/^Overview/)
    expect(tabs[1]).toMatch(/^Needs you/)
    expect(tabs[2]).toMatch(/^In flight/)
    expect(tabs[3]).toMatch(/^Issues/)
  })
})
