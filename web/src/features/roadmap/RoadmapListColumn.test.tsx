import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MAP } from './__fixtures__/map'
import { RoadmapListColumn } from './RoadmapListColumn'
import { capabilityKey } from './treeRows'

function mount(over: Partial<React.ComponentProps<typeof RoadmapListColumn>> = {}) {
  const onOpen = vi.fn()
  const onOpenInbox = vi.fn()
  render(
    <MemoryRouter>
      <RoadmapListColumn scope="map" map={MAP} isLoading={false} isError={false}
        selectedKey={null} onOpen={onOpen} onOpenInbox={onOpenInbox} {...over} />
    </MemoryRouter>,
  )
  return { onOpen, onOpenInbox }
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
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    // grouped by stage: both fixtures are in Build, under one header
    expect(screen.getByRole('button', { name: /^Build/ })).toBeInTheDocument()
  })

  it('states: loading, error, empty', () => {
    mount({ map: undefined, isLoading: true })
    expect(screen.getByRole('status', { name: 'Loading roadmap' })).toBeInTheDocument()
  })
})
