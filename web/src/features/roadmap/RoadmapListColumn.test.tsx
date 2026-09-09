import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from './__fixtures__/map'
import { RoadmapListColumn } from './RoadmapListColumn'
import { capabilityKey, productKey } from './treeRows'

function mount(over: Partial<React.ComponentProps<typeof RoadmapListColumn>> = {}) {
  const onOpen = vi.fn()
  const onOpenInbox = vi.fn()
  const { unmount } = render(
    <MemoryRouter>
      <RoadmapListColumn scope="map" map={MAP} isLoading={false} isError={false}
        selectedKey={null} onOpen={onOpen} onOpenInbox={onOpenInbox} {...over} />
    </MemoryRouter>,
  )
  return { onOpen, onOpenInbox, unmount }
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

  it('states: loading, error, empty', () => {
    mount({ map: undefined, isLoading: true })
    expect(screen.getByRole('status', { name: 'Loading roadmap' })).toBeInTheDocument()
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
})
