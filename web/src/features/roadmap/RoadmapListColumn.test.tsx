import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import type { RoadmapMap } from '@/api/roadmap'
import { MAP, cap, ini, product } from './__fixtures__/map'
import { RoadmapListColumn } from './RoadmapListColumn'
import { capabilityKey, initiativeKey, productKey } from './treeRows'
import { TREE_ID_SCOPE } from './useTreeKeys'
import { ViewportProvider } from '@/shell/useViewport'

// The Inbox scope's rows call the real triage mutations; the column's job here
// is only to mount that scope, so the hooks are stubbed rather than served.
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectProposal: () => ({ mutate: vi.fn(), isPending: false }),
  useAcceptBulk: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectBulk: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectDecision: () => ({ mutate: vi.fn(), isPending: false }),
  useTriageLive: () => undefined,
  useHolds: () => ({ data: undefined }),
  useUndoProposal: () => ({ mutate: vi.fn(), isPending: false }),
}))

const PROPOSALS: TriageProposal[] = [{
  proposalKind: 'triage', id: 'pr1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7,
  kind: 'issue', title: 'Recurring chores', url: null, proposedFeatureId: null,
  proposedFeatureSlug: 'tasks', noFitSuggestion: null, reach: 3, impact: 2, effort: 1,
  confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending', createdAt: null,
  classifiedBy: null, resolvedAt: null,
}]

function mount(over: Partial<React.ComponentProps<typeof RoadmapListColumn>> = {}) {
  const onOpen = vi.fn()
  const onOpenInbox = vi.fn()
  const onOpenProposal = vi.fn()
  const onClearProduct = vi.fn()
  const inbox = {
    proposals: PROPOSALS, isLoading: false, isError: false, product: null,
    onOpenProposal, onClearProduct,
  }
  const tree = (props: Partial<React.ComponentProps<typeof RoadmapListColumn>>) => (
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <RoadmapListColumn scope="map" map={MAP} isLoading={false} isError={false}
          selectedKey={null} onOpen={onOpen} onOpenInbox={onOpenInbox} inbox={inbox} {...props} />
      </MemoryRouter>
    </ViewportProvider>
  )
  const { unmount, rerender } = render(tree(over))
  /** Re-render the SAME tree with a prop changed — component state survives, which
   *  is the whole point for anything testing how the cursor reacts to a new
   *  `selectedKey` (an unmount/remount would reset it and prove nothing). */
  const setProps = (next: Partial<React.ComponentProps<typeof RoadmapListColumn>>) =>
    rerender(tree({ ...over, ...next }))
  return { onOpen, onOpenInbox, onOpenProposal, onClearProduct, unmount, setProps }
}

describe('RoadmapListColumn', () => {
  beforeEach(() => localStorage.clear())

  it('renders the Map scope as a tree with indented rows and opens a row on click', () => {
    const { onOpen } = mount()
    expect(screen.getByRole('navigation', { name: 'Roadmap scope' })).toBeInTheDocument()
    const tasks = screen.getByRole('button', { name: 'Open capability Tasks' })
    fireEvent.click(tasks)
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ kind: 'capability', key: capabilityKey('kindash', 'tasks') }))
    // depth is exposed for styling and assistive tech
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toHaveAttribute('aria-level', '3')
  })

  it('shows the error banner instead of throwing on a malformed map payload', () => {
    mount({ map: { generatedAt: 'x' } as unknown as RoadmapMap })
    expect(screen.getByText('Could not load the roadmap.')).toBeInTheDocument()
  })

  it('collapses a subtree from its toggle and remembers it', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Tasks' }))
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()
    // Folds go through `lib/groupCollapse`, so the key is that module's.
    expect(JSON.parse(localStorage.getItem('viberpit.groupCollapse.roadmap') ?? '[]'))
      .toContain(capabilityKey('kindash', 'tasks'))
  })

  it('a searched-open row reports itself open, never a dead Expand chevron', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Tasks' }))
    // The search force-expands the fold; the row must say so.
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search roadmap' }), { target: { value: 'task views' } })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeInTheDocument()
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'tasks')}`)).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Collapse Tasks' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Expand Tasks' })).toBeNull()
  })

  it('search narrows the tree', () => {
    mount()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search roadmap' }), { target: { value: 'billing' } })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
    expect(screen.queryByTestId(`row-${capabilityKey('viberpit', 'sessions')}`)).toBeNull()
  })

  it('the awaiting-triage pseudo row opens the inbox for that product', () => {
    const { onOpenInbox } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Open awaiting triage for KinDash' }))
    expect(onOpenInbox).toHaveBeenCalledWith('kindash')
  })

  it('the Initiatives scope lists every initiative grouped by stage', () => {
    mount({ scope: 'initiatives' })
    expect(screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open initiative No home yet' })).toBeInTheDocument()
    // The reason names the stage the gate would move to, not just "ready".
    expect(screen.getByText('Ready to advance → Deploy')).toBeInTheDocument()
    // grouped by stage: both fixtures are in Build, under one header
    expect(screen.getByRole('button', { name: /^Build/ })).toBeInTheDocument()
  })

  it('the Inbox scope is a third segment link, current, with the proposal rows under it', () => {
    const { onOpenProposal } = mount({ scope: 'inbox' })
    const link = screen.getByRole('link', { name: /^Inbox/ })
    expect(link).toHaveAttribute('aria-current', 'page')
    expect(link).toHaveAttribute('href', '/roadmap?scope=inbox')
    fireEvent.click(screen.getByRole('button', { name: 'Open proposal Recurring chores' }))
    expect(onOpenProposal).toHaveBeenCalledWith(expect.objectContaining({ id: 'pr1' }))
    // The tree belongs to the Map scope only — one scope body is mounted.
    expect(screen.queryByRole('tree')).toBeNull()
  })

  it('badges the queue depth on the Inbox link from every scope', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Inbox (1)' })).toBeInTheDocument()
  })

  // `proposals` is the WIDENED list: it also carries the triage loop's fresh
  // auto-accepts so the inbox can show the "auto-accepted · undo" chip. The
  // badge is a backlog count, and a decided row is not backlog.
  it('the Inbox badge excludes a fresh auto-accept in its undo window', () => {
    const proposals: TriageProposal[] = [
      ...['a', 'b', 'c', 'd', 'e'].map((id) => ({ ...PROPOSALS[0], id })),
      { ...PROPOSALS[0], id: 'auto', status: 'accepted', classifiedBy: 'auto' },
    ]
    mount({ inbox: { proposals, isLoading: false, isError: false, product: null,
                     onOpenProposal: vi.fn(), onClearProduct: vi.fn() } })
    expect(screen.getByRole('link', { name: 'Inbox (5)' })).toBeInTheDocument()
  })

  it('states: loading, error, empty', () => {
    mount({ map: undefined, isLoading: true })
    expect(screen.getByRole('status', { name: 'Loading roadmap' })).toBeInTheDocument()
  })

  it('the Initiatives scope lists phases with their program and hides program rows', () => {
    mount({ scope: 'initiatives' })
    // A program has no stage a human acts on directly — its phases do — so it
    // is not a row here at all.
    expect(screen.queryByTestId(`row-${initiativeKey('audit')}`)).toBeNull()
    const phase = screen.getByTestId(`row-${initiativeKey('audit-01')}`)
    // The program opens the phase's subtitle line as prose ("Part of UI audit ·
    // …") — no longer a trailing badge, which crowded the title to 0px on every
    // row (2026-09-30 Roadmap critique, P0). The title itself stays the identity.
    expect(within(phase).getByText(/· Part of UI audit$/)).toBeInTheDocument()
    expect(within(phase).getByRole('button', { name: /^Open initiative/ })).toBeInTheDocument()
  })

  it('a titleless program still yields a chip on the Initiatives scope, falling back to its slug', () => {
    const map: RoadmapMap = {
      ...MAP,
      products: [
        {
          ...MAP.products[0],
          initiatives: MAP.products[0].initiatives.map((i) => (i.slug === 'audit' ? { ...i, title: null } : i)),
        },
        MAP.products[1],
      ],
    }
    mount({ scope: 'initiatives', map })
    const phase = screen.getByTestId(`row-${initiativeKey('audit-01')}`)
    // The parent lookup resolves the program but finds no title on it — the
    // subtitle falls back to the program's own slug rather than disappearing.
    expect(within(phase).getByText(/· Part of audit$/)).toBeInTheDocument()
  })

  it('orders initiatives of one tier by priority, highest first, unscored last, then title', () => {
    const map: RoadmapMap = {
      ...MAP,
      products: [product({
        slug: 'kindash', name: 'KinDash',
        initiatives: [
          ini({ slug: 'low', title: 'Alpha', projectSlug: 'kindash', stage: 'build', priority: 5 }),
          ini({ slug: 'high', title: 'Zulu', projectSlug: 'kindash', stage: 'build', priority: 40 }),
          ini({ slug: 'none', title: 'Beta', projectSlug: 'kindash', stage: 'build', priority: null }),
        ],
      })],
    }
    mount({ scope: 'initiatives', map })
    const names = screen.getAllByRole('button', { name: /^Open initiative / })
      .map((b) => b.getAttribute('aria-label') ?? '')
    expect(names).toEqual(['Open initiative Zulu', 'Open initiative Alpha', 'Open initiative Beta'])
    // The row shows the number it sorted by.
    expect(screen.getByText(/priority 40/)).toBeInTheDocument()
  })

  it('badges a capability row with its open map-changes PR number', () => {
    const map: RoadmapMap = {
      ...MAP,
      products: [
        { ...MAP.products[0], capabilities: [cap({ slug: 'billing', title: 'Billing', prNumber: 12 })] },
        MAP.products[1],
      ],
    }
    mount({ map })
    const row = screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)
    expect(within(row).getByText('PR #12')).toBeInTheDocument()
    expect(within(row).getByText('proposed in pull request 12')).toBeInTheDocument()
  })

  it('a persona filter narrows the tree to that persona\'s capabilities', () => {
    mount()
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: 'Persona' }), { target: { value: 'teen' } })
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeNull()
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeInTheDocument()
  })

  it('clearing the persona filter (back to "All personas") restores the full tree', () => {
    mount()
    const select = screen.getByRole('combobox', { name: 'Persona' })
    fireEvent.change(select, { target: { value: 'teen' } })
    fireEvent.change(select, { target: { value: '' } })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'billing')}`)).toBeInTheDocument()
  })
})

/**
 * The tree's keyboard semantics (`useTreeKeys`). `role="tree"` is a promise: one
 * tab stop, arrows to walk it, `←`/`→` to fold it, and a row that says whether it
 * is the selected one. Before this the role was there and none of it was.
 */
describe('RoadmapListColumn — tree keyboard', () => {
  beforeEach(() => localStorage.clear())

  const control = (name: string) => screen.getByRole('button', { name })
  const productRow = productKey('kindash')

  it('marks the open row aria-selected, and only that one', () => {
    mount({ selectedKey: capabilityKey('kindash', 'tasks') })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'tasks')}`))
      .toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId(`row-${productRow}`)).toHaveAttribute('aria-selected', 'false')
  })

  it('gives the tree ONE tab stop: the open row, else the first row', () => {
    const { unmount } = mount({ selectedKey: capabilityKey('kindash', 'tasks') })
    expect(control('Open capability Tasks')).toHaveAttribute('tabindex', '0')
    expect(control('Open product KinDash')).toHaveAttribute('tabindex', '-1')
    unmount()

    mount()
    expect(control('Open product KinDash')).toHaveAttribute('tabindex', '0')
    expect(control('Open capability Tasks')).toHaveAttribute('tabindex', '-1')
  })

  it('ArrowDown and ArrowUp move focus between rows', () => {
    mount()
    const first = control('Open product KinDash')
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(control('Open capability Billing'))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(control('Open product KinDash'))
  })

  it('Home and End jump to the first and last row', () => {
    mount()
    const first = control('Open product KinDash')
    first.focus()
    fireEvent.keyDown(first, { key: 'End' })
    expect(document.activeElement).toBe(control('Open capability Sessions'))
    fireEvent.keyDown(document.activeElement!, { key: 'Home' })
    expect(document.activeElement).toBe(first)
  })

  it('ArrowRight expands a collapsed row, then steps onto its first child', () => {
    mount()
    const tasks = control('Open capability Tasks')
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Tasks' }))
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()

    fireEvent.keyDown(tasks, { key: 'ArrowRight' })
    expect(screen.getByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeInTheDocument()

    fireEvent.keyDown(control('Open capability Tasks'), { key: 'ArrowRight' })
    expect(document.activeElement).toBe(control('Open capability Task views'))
  })

  it('ArrowLeft collapses an expanded row, then moves to the parent', () => {
    mount()
    const tasks = control('Open capability Tasks')
    fireEvent.keyDown(tasks, { key: 'ArrowLeft' })
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()

    // Collapsed already: the second press hands focus up to the product.
    fireEvent.keyDown(control('Open capability Tasks'), { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(control('Open product KinDash'))
  })

  it('Enter opens the focused row', () => {
    const { onOpen } = mount()
    fireEvent.keyDown(control('Open capability Tasks'), { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onOpen).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'capability', key: capabilityKey('kindash', 'tasks') }),
    )
  })

  it('the Map scope folds a program with ArrowLeft', () => {
    mount()
    const program = screen.getByTestId(`row-${initiativeKey('audit')}`)
    expect(program).toHaveAttribute('aria-expanded', 'true')
    fireEvent.keyDown(program, { key: 'ArrowLeft' })
    expect(screen.queryByTestId(`row-${initiativeKey('audit-01')}`)).toBeNull()
  })

  // ── #1475: the three ways the one-tab-stop promise leaked ──────────────────

  it('the fold chevron is never a second tab stop on the roving row', () => {
    // The WAI-ARIA tree pattern is ONE tab stop for the whole tree. The chevron
    // used to rove with the row, so Tab into the tree landed on the chevron, Tab
    // again on the open control, and only the third Tab left. `←`/`→` fold from
    // the row, which is how a tree is folded.
    mount({ selectedKey: capabilityKey('kindash', 'tasks') })
    expect(control('Open capability Tasks')).toHaveAttribute('tabindex', '0')
    expect(control('Collapse Tasks')).toHaveAttribute('tabindex', '-1')
    // Still clickable — it is the POINTER affordance, not a keyboard one.
    fireEvent.click(control('Collapse Tasks'))
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()
  })

  it('the roving tab stop follows a new selection, not the last row arrowed to', () => {
    // `focusKey` used to be write-only: arrow down to a row, then open a
    // DIFFERENT one, and Tab took the user back to the row they had left rather
    // than to the one highlighted in front of them. Selection arrives as a prop
    // (it is the URL), so a new selection is a new `selectedKey`.
    const { setProps } = mount({ selectedKey: productKey('kindash') })
    const first = control('Open product KinDash')
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(control('Open capability Billing')).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('tabindex', '-1')

    setProps({ selectedKey: capabilityKey('kindash', 'tasks') })
    expect(control('Open capability Tasks')).toHaveAttribute('tabindex', '0')
    expect(control('Open capability Billing')).toHaveAttribute('tabindex', '-1')
  })

  it('a walk through the tree still keeps its cursor while the selection holds', () => {
    // The other half of the rule: only a CHANGED selection retires the cursor.
    // Arrowing does not open a row, so two arrow presses must not lose the stop.
    const { setProps } = mount({ selectedKey: productKey('kindash') })
    const first = control('Open product KinDash')
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    setProps({ selectedKey: productKey('kindash') })   // a re-render, same selection
    expect(control('Open capability Billing')).toHaveAttribute('tabindex', '0')
  })

  it('namespaces its row ids so an identically-keyed row elsewhere cannot steal focus', () => {
    // A detail pane rendering the same initiative mints the same `rowKey`. With
    // both unscoped, `document.getElementById` returned whichever came first in
    // the DOM — so arrow-key focus was correct only by the accident of pane order.
    mount()
    const row = control('Open capability Tasks')
    expect(row.id).toBe(`row-${TREE_ID_SCOPE}-${capabilityKey('kindash', 'tasks')}`)
    // And the focus path still lands: the lookup uses the same namespace.
    const first = control('Open product KinDash')
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(control('Open capability Billing'))
  })
})

// ── the Map pill carries the queue's attention (2026-09-30 Roadmap critique, P1) ──
// On a phone `/roadmap` renders only this column; the portfolio's Needs-you list
// lives in the detail pane, which a phone shows only with an item open. The
// pill's count stays the product inventory; the dot says the map holds a queue.
describe('RoadmapListColumn — the Map pill and the queue', () => {
  it('marks Map as needing attention while any product has needs-you work', () => {
    mount({ scope: 'map' })
    // The fixture's kindash product carries a needs-you initiative.
    expect(screen.getByRole('link', { name: /^Map \(\d+, needs attention\)$/ })).toBeInTheDocument()
  })
})
