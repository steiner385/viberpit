import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { MAP, product } from '../__fixtures__/map'
import { PortfolioDetail } from './PortfolioDetail'
import { needsYouCount } from './needsYou'
import { ViewportProvider } from '@/shell/useViewport'

// Every hook the tree under test reaches: `RankedList` calls `useQueue`, and
// `NeedsYouList` mounts `IncidentQueue`, which calls the two incident hooks.
vi.mock('@/api/hooks', () => ({
  useHolds: () => ({ data: undefined }),
  useOwnerDigest: () => ({ data: undefined }),
  useApplyDigest: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useQueue: () => ({
    data: {
      entries: [
        { ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, product: 'kindash', title: 'Top', url: 'https://x/1', score: 9.5 },
        { ref: 'steiner385/viberpit#2', repo: 'steiner385/viberpit', number: 2, product: 'viberpit', title: 'Second', url: 'https://x/2', score: 4 },
        { ref: 'steiner385/viberpit#3', repo: 'steiner385/viberpit', number: 3, product: 'viberpit', title: 'No link', url: null, score: null },
      ],
      skipped: 0,
      generatedAt: 'now',
    },
    isLoading: false,
  }),
  useIncidents: () => ({ data: [], isError: false }),
  useIncidentAction: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))

function Here() {
  const loc = useLocation()
  return <div data-testid="here">{loc.pathname + loc.search}</div>
}

function mount(initial = '/roadmap') {
  render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter initialEntries={[initial]}>
        <PortfolioDetail map={MAP} />
        <Here />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

const here = () => screen.getByTestId('here').textContent

/** The stat tile for a label. Matched on the `dt` because "Needs you" is also a
 *  tab label, and the tab would otherwise match too. */
const tile = (label: string) =>
  screen.getAllByText(label).find((el) => el.closest('dt'))?.closest('div')

describe('PortfolioDetail', () => {
  it('shows stat tiles and the Needs-you tab by default', () => {
    mount()
    expect(screen.getByText('Products')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Needs you/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('No home yet')).toBeInTheDocument() // needs-you initiative
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    // The queue is a SCOPE of this section now, not a section of its own.
    expect(screen.getByRole('link', { name: /2 awaiting triage/ }))
      .toHaveAttribute('href', '/roadmap?scope=inbox&product=kindash')
  })

  it('counts the whole portfolio in the tiles', () => {
    mount()
    // In flight = the INITIATIVES that are moving or stuck (`inFlight.ts`), the
    // same population the product and capability levels count under that word:
    // KinDash's `views` (in-flight), `orphan` (needs-you), and the `audit`
    // program's `audit-01` phase — the `audit` program itself is excluded
    // (leaf-only), so this is 3, not 4. The server's `counts.inFlight` rolls up
    // issue tiers too, so summing it here made one label mean two different
    // things one level apart.
    expect(tile('In flight')).toHaveTextContent('3')
    expect(tile('Blocked')).toHaveTextContent('0')
    expect(tile('Products')).toHaveTextContent('2')
  })

  it('badges Needs you with the rows the list actually renders', () => {
    // The needs-you initiative AND KinDash's awaiting-triage row — `counts.needsYou`
    // alone would say 1 over a list of 2.
    expect(needsYouCount(MAP.products)).toBe(2)
    expect(needsYouCount([])).toBe(0)
    expect(needsYouCount([product({ slug: 'quiet' })])).toBe(0)
    mount()
    expect(screen.getByRole('tab', { name: 'Needs you (2, needs attention)' })).toBeInTheDocument()
    expect(tile('Needs you')).toHaveTextContent('2')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('Ranked tab lists the queue across products in score order', () => {
    mount('/roadmap?tab=ranked')
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('Top')
    expect(rows[1]).toHaveTextContent('Second')
    expect(screen.getByRole('link', { name: /#1 Top/ })).toHaveAttribute('href', 'https://x/1')
    // An entry with no URL is text, not a link that goes nowhere.
    expect(rows[2]).toHaveTextContent('No link')
    expect(screen.queryByRole('link', { name: /#3 No link/ })).toBeNull()
  })

  it('All initiatives tab lists every initiative and opens one', () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /All initiatives/ }))
    expect(here()).toBe('/roadmap?tab=initiatives')
    const open = screen.getByRole('button', { name: 'Open initiative Use-case-first task views' })
    expect(screen.getByRole('button', { name: 'Open initiative No home yet' })).toBeInTheDocument()
    fireEvent.click(open)
    expect(here()).toBe('/initiatives/views')
  })
})

// ── the queue leads (2026-09-30 Roadmap critique, P1; docs/PAGE-TABS.md §4) ──
describe('PortfolioDetail — the queue leads', () => {
  it('tiles run Needs you · Blocked · In flight · Products; Needs you is the first tab', () => {
    mount()
    const tiles = [...document.querySelectorAll('dl dt')].map((dt) => dt.textContent?.trim())
    expect(tiles).toEqual(['Needs you', 'Blocked', 'In flight', 'Products'])
    const tabs = screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label') ?? t.textContent ?? '')
    expect(tabs[0]).toMatch(/^Needs you/)
  })
})
