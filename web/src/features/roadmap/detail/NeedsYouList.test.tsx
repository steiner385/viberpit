import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { ProductNode } from '@/api/roadmap'
import { cap, product } from '../__fixtures__/map'
import { NeedsYouList } from './NeedsYouList'

vi.mock('@/initiatives/IncidentQueue', () => ({ IncidentQueue: () => null }))
vi.mock('./LeafList', () => ({ LeafList: ({ scope, attentionOnly, product }: { scope: { capability: string }; attentionOnly: boolean; product: ProductNode }) => (
  <div aria-label="Attention issue actions">{scope.capability} {String(attentionOnly)} {product.capabilities.map((c) => c.slug).join(",")}</div>
) }))

const child = cap({ slug: 'child', title: 'Child capability', counts: { openIssues: 2, inFlight: 0, needsYou: 1, blocked: 1 } })
const root = cap({ slug: 'root', title: 'Root capability', children: [child], counts: { openIssues: 3, inFlight: 0, needsYou: 2, blocked: 1 } })
const products = [product({ slug: 'p', capabilities: [root] })]

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
