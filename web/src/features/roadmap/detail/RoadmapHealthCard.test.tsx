// The product overview's hygiene notice: capabilities and initiatives the map
// nesting doesn't reach, capabilities still `proposed` after the map-changes PR
// merged, and inbox items no one triaged. Renders nothing when there is nothing
// to say — this is a notice, not a permanent fixture.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RoadmapHealthCard } from './RoadmapHealthCard'

const health = vi.hoisted(() => ({ data: undefined as unknown, isLoading: false }))

vi.mock('@/api/hooks', () => ({
  useProjectHealth: () => health,
}))

const mount = () =>
  render(
    <MemoryRouter>
      <RoadmapHealthCard productSlug="kindash" />
    </MemoryRouter>,
  )

describe('RoadmapHealthCard', () => {
  it('lists each non-empty finding, with capability/initiative links', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: ['flaky-test-triage'],
        unlinkedInitiatives: [],
        staleProposed: ['flaky-test-triage'],
        staleInbox: 3,
        unindexedDocs: [],
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText('Roadmap hygiene')).toBeInTheDocument()
    expect(screen.getByText(/1 capability outside the tree/)).toBeInTheDocument()
    expect(screen.getByText(/1 stale proposed capability/)).toBeInTheDocument()
    expect(screen.getByText(/3 inbox items older than 7 days/)).toBeInTheDocument()
    // No "leaf initiatives" line: unlinkedInitiatives is empty.
    expect(screen.queryByText(/leaf initiative/)).toBeNull()
    // The fixture's slug appears in both findings, so both links resolve.
    const links = screen.getAllByRole('link', { name: 'flaky-test-triage' })
    expect(links.length).toBeGreaterThan(0)
    for (const link of links) {
      expect(link).toHaveAttribute('href', '/roadmap/kindash/capabilities/flaky-test-triage')
    }
  })

  it('renders nothing when every list is empty and staleInbox is 0', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        generatedAt: 'x',
      },
    })
    const { container } = mount()
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing while loading', () => {
    Object.assign(health, { data: undefined, isLoading: true })
    const { container } = mount()
    expect(container.firstChild).toBeNull()
  })

  it('lists unparseable feature docs even when every other field is empty', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [
          { path: 'docs/product/features/bad.md', error: 'unterminated frontmatter fence' },
        ],
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText('Roadmap hygiene')).toBeInTheDocument()
    expect(screen.getByText(/1 feature doc could not be parsed/)).toBeInTheDocument()
    expect(screen.getByText('docs/product/features/bad.md')).toBeInTheDocument()
  })

  it('surfaces a missing constitution as a governance bullet', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: 'missing',
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText('Roadmap hygiene')).toBeInTheDocument()
    expect(screen.getByText(/no ratified constitution/i)).toBeInTheDocument()
  })

  it('stays hidden when governance is null and everything else is empty', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: null,
        generatedAt: 'x',
      },
    })
    const { container } = mount()
    expect(container.firstChild).toBeNull()
  })
})
