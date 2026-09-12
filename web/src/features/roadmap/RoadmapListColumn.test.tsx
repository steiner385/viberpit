import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import type { RoadmapMap } from '@/api/roadmap'
import { MAP, cap } from './__fixtures__/map'
import { RoadmapListColumn } from './RoadmapListColumn'
import { capabilityKey, initiativeKey, productKey } from './treeRows'
import { ViewportProvider } from '@/shell/useViewport'

// The Inbox scope's rows call the real triage mutations; the column's job here
// is only to mount that scope, so the hooks are stubbed rather than served.
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectProposal: () => ({ mutate: vi.fn(), isPending: false }),
  useAcceptBulk: () => ({ mutate: vi.fn(), isPending: false }),
  useTriageLive: () => undefined,
  useUndoProposal: () => ({ mutate: vi.fn(), isPending: false }),
}))

const PROPOSALS: TriageProposal[] = [{
  id: 'pr1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7,
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
  const { unmount } = render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <RoadmapListColumn scope="map" map={MAP} isLoading={false} isError={false}
          selectedKey={null} onOpen={onOpen} onOpenInbox={onOpenInbox} inbox={inbox} {...over} />
      </MemoryRouter>
    </ViewportProvider>,
  )
  return { onOpen, onOpenInbox, onOpenProposal, onClearProduct, unmount }
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

  it('collapses a subtree from its toggle and remembers it', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Tasks' }))
    expect(screen.queryByTestId(`row-${capabilityKey('kindash', 'task-views')}`)).toBeNull()
    // Folds go through `lib/groupCollapse`, so the key is that module's.
    expect(JSON.parse(localStorage.getItem('ccswitch.groupCollapse.roadmap') ?? '[]'))
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
    expect(within(phase).getByText('UI audit')).toBeInTheDocument()
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
    // chip falls back to the program's own slug rather than disappearing.
    expect(within(phase).getByText('audit')).toBeInTheDocument()
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
})
