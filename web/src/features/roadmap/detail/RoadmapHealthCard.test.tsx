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

  it('lists capability evidence gaps, linking each back to its capability (#2162)', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: null,
        danglingEvidence: [{ capability: 'task-views', kind: 'code', path: 'server/old.ts' }],
        untestedCapabilities: ['task-views', 'calendar'],
        codeWithoutSpecs: ['calendar'],
        stateBehindSpec: ['task-views'],
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText(/1 cited evidence path no longer on the default branch/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'server/old.ts' }))
      .toHaveAttribute('href', '/roadmap/kindash/capabilities/task-views')
    expect(screen.getByText(/2 live capabilities cite no tests/)).toBeInTheDocument()
    expect(screen.getByText(/1 capability cites code but no spec/)).toBeInTheDocument()
    expect(screen.getByText(/1 capability has a state older than its newest spec/)).toBeInTheDocument()
  })

  it('lists artifact frontmatter findings, linking back to the initiative (#2161)', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: null,
        artifactsEpicClosedNotDone: ['task-views'],
        artifactsUnknownCapability: [{ initiative: 'task-views', capability: 'ghost-capability' }],
        artifactsMissingFrontmatter: ['old-init'],
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText(/1 initiative has a closed epic but isn't marked done/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'task-views' }))
      .toHaveAttribute('href', '/initiatives/task-views')
    expect(screen.getByText(/1 artifact frontmatter capability names no indexed feature/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ghost-capability' }))
      .toHaveAttribute('href', '/initiatives/task-views')
    expect(screen.getByText(/1 initiative has a committed artifact with no frontmatter/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'old-init' }))
      .toHaveAttribute('href', '/initiatives/old-init')
  })

  it('lists unlinked personas, linking each back to its capability (#2164)', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: null,
        danglingEvidence: [],
        untestedCapabilities: [],
        codeWithoutSpecs: [],
        stateBehindSpec: [],
        unlinkedPersonas: [{ capability: 'chores', persona: 'ghost' }],
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText(/1 capability cites an unindexed persona/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ghost' }))
      .toHaveAttribute('href', '/roadmap/kindash/capabilities/chores')
  })
})
