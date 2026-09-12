// The product's own dependencies (spec §8 "Product dependency"): chips for the
// edges it has, a remove on each, and one row to add another.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ProductNode } from '@/api/roadmap'
import { ProductEdgesEditor } from './ProductEdgesEditor'

const h = vi.hoisted(() => ({
  create: vi.fn(), remove: vi.fn(),
  project: {
    id: 'p1', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
    onboardingState: 'live', createdAt: null, updatedAt: null, repos: [], featureCount: 0, workItemCount: 0,
    edges: [{ id: 'e1', srcSlug: 'kindash', dstSlug: 'web', kind: 'depends_on', note: null }],
  },
  map: { generatedAt: 'now', products: [
    { slug: 'kindash', name: 'KinDash' }, { slug: 'web', name: 'Web' }, { slug: 'infra', name: 'Infra' },
  ] },
}))
vi.mock('@/api/hooks', () => ({
  useProject: () => ({ data: h.project, isLoading: false, isError: false }),
  useRoadmapMap: () => ({ data: h.map }),
  useCreateEdge: () => ({ mutate: h.create, isPending: false }),
  useDeleteEdge: () => ({ mutate: h.remove, isPending: false }),
}))

const product = { slug: 'kindash', name: 'KinDash' } as ProductNode

describe('ProductEdgesEditor', () => {
  beforeEach(() => { h.create.mockReset(); h.remove.mockReset() })

  it('lists the edges as chips with a remove each', () => {
    render(<ProductEdgesEditor product={product} />)
    expect(screen.getByText('depends on Web')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove: depends on Web' }))
    expect(h.remove).toHaveBeenCalledWith('e1')
  })

  it('adds an edge to another product; the product itself and existing targets are not offered', () => {
    render(<ProductEdgesEditor product={product} />)
    const target = screen.getByLabelText('Product')
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    expect(screen.queryByRole('option', { name: 'KinDash' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Relationship'), { target: { value: 'deploys_via' } })
    fireEvent.change(target, { target: { value: 'infra' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(h.create).toHaveBeenCalledWith({ dstSlug: 'infra', kind: 'deploys_via' }, expect.anything())
  })
})
