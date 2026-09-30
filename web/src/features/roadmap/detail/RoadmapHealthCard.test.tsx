// The product overview's hygiene notice: capabilities and initiatives the map
// nesting doesn't reach, capabilities still `proposed` after the map-changes PR
// merged, and inbox items no one triaged. Renders nothing when there is nothing
// to say — this is a notice, not a permanent fixture.
//
// Collapsed by default to one line of counts (2026-09-30 Roadmap critique, P1):
// the title is the toggle; the full list, links included, is one click away.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

const toggle = () => screen.getByRole('button', { name: /^Roadmap hygiene/ })
/** Expand the notice — every "lists …" case below reads the bullets, which are
 *  behind the one-line summary. */
const reveal = () => fireEvent.click(toggle())

const EMPTY = {
  unparentedCapabilities: [], unlinkedInitiatives: [], staleProposed: [], staleInbox: 0,
  unindexedDocs: [], generatedAt: 'x',
}

describe('RoadmapHealthCard — collapsed to one line', () => {
  it('opens on a summary of counts, not the list; the toggle expands it', () => {
    Object.assign(health, {
      data: { ...EMPTY, unparentedCapabilities: ['flaky-test-triage'], staleProposed: ['flaky-test-triage'], staleInbox: 3 },
    })
    mount()
    const btn = toggle()
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(btn).toHaveTextContent('Roadmap hygiene — 1 capability outside the tree · 1 stale proposed capability · 3 stale inbox items')
    // Nothing below the line until asked: no list, no links.
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.queryByRole('link')).toBeNull()
    reveal()
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('list')).toHaveAttribute('id', btn.getAttribute('aria-controls')!)
    expect(screen.getAllByRole('link', { name: 'flaky-test-triage' }).length).toBeGreaterThan(0)
  })

  it('names the first three findings and counts the rest', () => {
    Object.assign(health, {
      data: {
        ...EMPTY, governance: 'missing',
        danglingEvidence: [{ capability: 'a', kind: 'code', path: 'x.ts' }, { capability: 'a', kind: 'code', path: 'y.ts' }],
        untestedCapabilities: ['a', 'b', 'c'], codeWithoutSpecs: ['a'], unlinkedPersonas: [{ capability: 'a', persona: 'p' }],
      },
    })
    mount()
    expect(toggle()).toHaveTextContent('Roadmap hygiene — governance · 2 stale evidence paths · 3 without tests · +2 more')
  })
})

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
    reveal()
    expect(screen.getByText(/1 capability outside the tree:/)).toBeInTheDocument()
    expect(screen.getByText(/1 stale proposed capability:/)).toBeInTheDocument()
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
    Object.assign(health, { data: { ...EMPTY } })
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
      data: { ...EMPTY, unindexedDocs: [{ path: 'docs/product/features/bad.md', error: 'unterminated frontmatter fence' }] },
    })
    mount()
    expect(toggle()).toHaveTextContent('1 unparsed doc')
    reveal()
    expect(screen.getByText(/1 feature doc could not be parsed/)).toBeInTheDocument()
    expect(screen.getByText('docs/product/features/bad.md')).toBeInTheDocument()
  })

  it('surfaces a missing constitution as a governance bullet', () => {
    Object.assign(health, { data: { ...EMPTY, governance: 'missing' } })
    mount()
    expect(toggle()).toHaveTextContent('Roadmap hygiene — governance')
    reveal()
    expect(screen.getByText(/no ratified constitution/i)).toBeInTheDocument()
  })

  it('stays hidden when governance is null and everything else is empty', () => {
    Object.assign(health, { data: { ...EMPTY, governance: null } })
    const { container } = mount()
    expect(container.firstChild).toBeNull()
  })

  it('lists capability evidence gaps, linking each back to its capability (#2162)', () => {
    Object.assign(health, {
      data: {
        ...EMPTY, governance: null,
        danglingEvidence: [{ capability: 'task-views', kind: 'code', path: 'server/old.ts' }],
        untestedCapabilities: ['task-views', 'calendar'],
        codeWithoutSpecs: ['calendar'],
        stateBehindSpec: ['task-views'],
      },
    })
    mount()
    reveal()
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
        ...EMPTY, governance: null,
        artifactsEpicClosedNotDone: ['task-views'],
        artifactsUnknownCapability: [{ initiative: 'task-views', capability: 'ghost-capability' }],
        artifactsMissingFrontmatter: ['old-init'],
      },
    })
    mount()
    reveal()
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
        ...EMPTY, governance: null,
        danglingEvidence: [], untestedCapabilities: [], codeWithoutSpecs: [], stateBehindSpec: [],
        unlinkedPersonas: [{ capability: 'chores', persona: 'ghost' }],
      },
    })
    mount()
    reveal()
    expect(screen.getByText(/1 unresolved persona reference/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ghost' }))
      .toHaveAttribute('href', '/roadmap/kindash/capabilities/chores')
  })
})
