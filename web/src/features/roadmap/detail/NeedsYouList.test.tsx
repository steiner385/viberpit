import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { ProductNode } from '@/api/roadmap'
import { cap, ini, product } from '../__fixtures__/map'
import { NeedsYouList } from './NeedsYouList'

vi.mock('@/initiatives/IncidentQueue', () => ({ IncidentQueue: () => null }))
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
