import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { ProductNode } from '@/api/roadmap'
import { cap, ini, product } from '../__fixtures__/map'
import { NeedsYouList } from './NeedsYouList'

vi.mock('@/initiatives/IncidentQueue', () => ({ IncidentQueue: () => null }))
const h = vi.hoisted(() => ({ held: [] as unknown[], calls: [] as unknown[] }))
vi.mock('@/api/hooks', () => ({
  useHolds: (product: string | undefined, opts: { enabled: boolean }) => { h.calls.push([product, opts.enabled]); return { data: { products: [], held: h.held, truncated: 0, generatedAt: '' } } },
}))
vi.mock('./HeldIssueCard', () => ({ HeldIssueCard: ({ item, showProduct }: { item: { ref: string; product: string }; showProduct: boolean }) => (
  <li data-testid="held-issue">{item.ref} {item.product} {String(showProduct)}</li>
) }))
vi.mock('./LeafList', () => ({ LeafList: ({ scope, attentionOnly, product }: { scope: { capability: string }; attentionOnly: boolean; product: ProductNode }) => (
  <div aria-label="Attention issue actions">{scope.capability} {String(attentionOnly)} {product.capabilities.map((c) => c.slug).join(",")}</div>
) }))

const child = cap({ slug: 'child', title: 'Child capability', counts: { openIssues: 2, inFlight: 0, needsYou: 1, blocked: 1 } })
const root = cap({ slug: 'root', title: 'Root capability', children: [child], counts: { openIssues: 3, inFlight: 0, needsYou: 2, blocked: 1 } })
const products = [product({ slug: 'p', capabilities: [root] })]

describe('NeedsYouList initiative rows — the CTA names the move, not "Review"', () => {
  it('says what each row is waiting for and what the tap does', () => {
    const p = product({ slug: 'p', name: 'Pea', capabilities: [], initiatives: [
      ini({ slug: 'ship', title: 'Ship it', projectSlug: 'p', tier: 'needs-you', reason: 'advance', stage: 'build' }),
      ini({ slug: 'spec', title: 'Spec it', projectSlug: 'p', tier: 'needs-you', status: 'awaiting_human', stage: 'design' }),
      ini({ slug: 'held', title: 'Held work', projectSlug: 'p', tier: 'needs-you', status: 'active', stage: 'build',
            epic: { ref: null, url: null, total: 3, done: 0, blocked: 0, held: 2 } }),
      ini({ slug: 'stuck', title: 'Stuck', projectSlug: 'p', tier: 'blocked', reason: 'epic_blocked', status: 'active', stage: 'build' }),
    ] })
    render(<MemoryRouter><NeedsYouList products={[p]} /></MemoryRouter>)
    const list = screen.getByRole('list', { name: 'Initiatives that need you' })
    const rows = [...list.querySelectorAll('li')].map((li) => li.textContent)
    // needs-you tier first, then blocked; slug order inside a tier (no updatedAt).
    expect(rows).toEqual([
      'Held workPea · 2 issues held for youReview 2 held issues',
      'Ship itPea · Ready to advanceAdvance → Deploy',
      'Spec itPea · Awaiting humanReview the spec',
      'StuckPea · Blocked by open dependenciesClear the blocking dependencies',
    ])
    expect(screen.getByRole('link', { name: 'Review the spec' })).toHaveAttribute('href', '/initiatives/spec')
    expect(screen.queryByText(/requested input/)).not.toBeInTheDocument()
  })
})

describe('NeedsYouList issue groups', () => {
  it('offers capability attention links without requesting issue lists for every capability', () => {
    render(<MemoryRouter><NeedsYouList products={products} /></MemoryRouter>)
    expect(screen.getByRole('link', { name: 'Review issues in Root capability' })).toHaveAttribute('href', '/roadmap/p/capabilities/root?tab=needs-you')
    expect(screen.getByRole('link', { name: 'Review issues in Child capability' })).toHaveAttribute('href', '/roadmap/p/capabilities/child?tab=needs-you')
    expect(screen.queryByText('Nothing needs you')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Attention issue actions')).not.toBeInTheDocument()
  })

  it('preserves the full product for issue reassignment within a scoped attention list', () => {
    const fullProduct = product({ slug: 'p', capabilities: [root, cap({ slug: 'other' })] })
    render(<MemoryRouter><NeedsYouList products={[fullProduct]} capability={root} /></MemoryRouter>)
    expect(screen.getByLabelText('Attention issue actions')).toHaveTextContent('root,other')
  })

  it('renders actionable attention issues for the selected capability, keeping children navigable', () => {
    render(<MemoryRouter><NeedsYouList products={products} capability={root} /></MemoryRouter>)
    expect(screen.getByLabelText('Attention issue actions')).toHaveTextContent('f-root true')
    expect(screen.queryByRole('link', { name: 'Review issues in Root capability' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review issues in Child capability' })).toBeInTheDocument()
  })
})

describe('NeedsYouList held issues — the escalation read model, not the map counts', () => {
  const heldRow = (ref: string, product: string) => ({ ref, product, number: Number(ref.split('#')[1]), workItemId: `w-${product}` })

  it('lists every held issue of the product, including one no capability counts, and is not "Nothing needs you"', () => {
    h.calls.length = 0
    // ghostdriver-mcp#28 (2026-10-02): held, unclassified — the map says needsYou 0.
    h.held = [heldRow('steiner385/ghostdriver-mcp#28', 'ghostdriver')]
    render(<MemoryRouter><NeedsYouList products={[product({ slug: 'ghostdriver', counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 2, awaitingTriage: 0, needsHome: 1 } })]} /></MemoryRouter>)
    expect(h.calls).toEqual([['ghostdriver', true]])
    expect(screen.getByText('Held issues · 1')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Held issues' })).toHaveTextContent('steiner385/ghostdriver-mcp#28 ghostdriver false')
    expect(screen.queryByText('Nothing needs you')).not.toBeInTheDocument()
  })

  it('reads fleet-wide for a portfolio, keeps only products in scope, and names the product on each card', () => {
    h.calls.length = 0
    h.held = [heldRow('o/a#1', 'alpha'), heldRow('o/z#9', 'zeta')]
    render(<MemoryRouter><NeedsYouList products={[product({ slug: 'alpha' }), product({ slug: 'beta' })]} /></MemoryRouter>)
    expect(h.calls).toEqual([[undefined, true]])
    const list = screen.getByRole('list', { name: 'Held issues' })
    expect(list).toHaveTextContent('o/a#1 alpha true')
    expect(list).not.toHaveTextContent('o/z#9')
  })

  it('skips the read in a capability scope, whose own issue rows carry the hold', () => {
    h.calls.length = 0
    h.held = [heldRow('o/p#1', 'p')]
    render(<MemoryRouter><NeedsYouList products={products} capability={root} /></MemoryRouter>)
    expect(h.calls).toEqual([['p', false]])
    expect(screen.queryByRole('list', { name: 'Held issues' })).not.toBeInTheDocument()
  })

  it('still says nothing needs you when nothing is held either', () => {
    h.held = []
    render(<MemoryRouter><NeedsYouList products={[product({ slug: 'quiet' })]} /></MemoryRouter>)
    expect(screen.getByText('Nothing needs you')).toBeInTheDocument()
  })
})
