// The bottom of the tree renders through a window: a capability with hundreds
// of issues mounts a handful of rows, and the two section headers travel with
// the leaves inside the same virtualizer.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Leaf } from '@/api/roadmap'
import { LeafList } from './LeafList'

const h = vi.hoisted(() => ({ leaves: [] as unknown[], isLoading: false, isError: false }))
vi.mock('@/api/hooks', () => ({
  useLeaves: () => ({ data: h.leaves, isLoading: h.isLoading, isError: h.isError }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

const leaf = (n: number, state: string, extra: Partial<Leaf> = {}): Leaf => ({
  id: `w${n}`, ref: `o/r#${n}`, repo: 'o/r', number: n, kind: 'issue', title: `Issue ${n}`, url: null, state,
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null,
  reason: null, bounces: 0, ...extra,
})

// The virtualizer's 150 ms scroll-reset timer must fire before jsdom's window
// goes away (see TranscriptView.test.tsx for the same flush).
afterEach(() => new Promise((r) => setTimeout(r, 200)))

describe('LeafList', () => {
  it('renders both sections with their counts and every row of a short list', () => {
    h.leaves = [leaf(1, 'open'), leaf(2, 'open'), leaf(3, 'closed')]
    render(<div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div>)
    expect(screen.getByText('Open · 2')).toBeInTheDocument()
    expect(screen.getByText('Closed · 1')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(5)   // 2 headers + 3 leaves
    expect(screen.getByText('#3 Issue 3')).toBeInTheDocument()
  })

  it('mounts a window, not the whole list, for a long one', () => {
    h.leaves = Array.from({ length: 300 }, (_, i) => leaf(i + 1, 'open'))
    render(<div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div>)
    const items = screen.getAllByRole('listitem')
    expect(items.length).toBeGreaterThan(1)
    expect(items.length).toBeLessThan(100)
    expect(screen.queryByText('#300 Issue 300')).toBeNull()
    // Only a window is mounted, so each row carries its place in the WHOLE list
    // (1 header + 300 leaves); without it assistive tech announces "1 of 11".
    expect(items[0]).toHaveAttribute('aria-posinset', '1')
    expect(items[0]).toHaveAttribute('aria-setsize', '301')
    expect(items[1]).toHaveAttribute('aria-posinset', '2')
  })

  it('says so when there is nothing', () => {
    h.leaves = []
    render(<LeafList scope={{ capability: 'f1' }} product={null} />)
    expect(screen.getByText('No issues here yet')).toBeInTheDocument()
  })

  it('shows the bounce chip for a needs_spec leaf and none for a leaf with no reason', () => {
    h.leaves = [
      leaf(1, 'open', { reason: 'needs_spec', bounces: 2, tier: 'needs-you' }),
      leaf(2, 'open', { reason: null, bounces: 0 }),
    ]
    render(<div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div>)
    expect(screen.getByText('bounced 2× — needs spec')).toBeInTheDocument()
    // Only one chip rendered — the second leaf has no reason.
    expect(screen.getAllByText(/needs spec/)).toHaveLength(1)
  })
})
