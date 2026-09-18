// The bottom of the tree renders through a window: a capability with hundreds
// of issues mounts a handful of rows, and the two section headers travel with
// the leaves inside the same virtualizer.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import type { Leaf } from '@/api/roadmap'
import { LeafList } from './LeafList'
import { IssueDispositionProvider } from './IssueDispositionProvider'

const h = vi.hoisted(() => ({ leaves: [] as unknown[], isLoading: false, isError: false }))
vi.mock('@/api/hooks', () => ({
  useLeaves: () => ({ data: h.leaves, isLoading: h.isLoading, isError: h.isError }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

vi.mock('./IssueDispositionDialog', () => ({ IssueDispositionDialog: ({ itemId }: { itemId: string }) => <div role="dialog">Review {itemId}</div> }))

const leaf = (n: number, state: string, extra: Partial<Leaf> = {}): Leaf => ({
  id: `w${n}`, ref: `o/r#${n}`, repo: 'o/r', number: n, kind: 'issue', title: `Issue ${n}`, url: null, state,
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null,
  reason: null, bounces: 0, holdKind: null, holdReason: null, holdBy: null, holdSetAt: null, ...extra,
})

// The virtualizer's 150 ms scroll-reset timer must fire before jsdom's window
// goes away (see TranscriptView.test.tsx for the same flush).
afterEach(() => new Promise((r) => setTimeout(r, 200)))

describe('LeafList', () => {
  it('renders both sections with their counts and every row of a short list', () => {
    h.leaves = [leaf(1, 'open'), leaf(2, 'open'), leaf(3, 'closed')]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    expect(screen.getByText('Open · 2')).toBeInTheDocument()
    expect(screen.getByText('Closed · 1')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(5)   // 2 headers + 3 leaves
    expect(screen.getByText('#3 Issue 3')).toBeInTheDocument()
  })

  it('mounts a window, not the whole list, for a long one', () => {
    h.leaves = Array.from({ length: 300 }, (_, i) => leaf(i + 1, 'open'))
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
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
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    const row = (n: number) => within(screen.getByText(`#${n} Issue ${n}`).closest('[role="listitem"]')!)
    expect(row(1).getByTestId('leaf-reason')).toHaveTextContent('bounced 2× — needs spec')
    // Absence of the ELEMENT inside the second row, not of the words: without the
    // `leaf.reason &&` guard every ordinary leaf renders an EMPTY warning pill,
    // which any text query happily reports as "no chip".
    expect(row(2).queryByTestId('leaf-reason')).toBeNull()
  })
  it('explains missing requirements with a secondary GitHub link', () => {
    h.leaves = [leaf(1, 'open', { tier: 'needs-you', reason: 'needs_spec', url: 'https://github.com/o/r/issues/1' })]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    expect(screen.getByText('Repeated attempts have not merged. Update the issue description with clearer requirements and acceptance criteria before another attempt.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View on GitHub' })).toHaveAttribute('href', 'https://github.com/o/r/issues/1')
  })

  it('gives generic guidance without inventing a session link or missing issue URL', () => {
    h.leaves = [leaf(1, 'open', { tier: 'needs-you', sessionStatus: 'waiting' })]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    expect(screen.getByText('Read the issue, update its details, or close it as resolved or won’t do. If a session needs input, respond in Sessions.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'View on GitHub' })).not.toBeInTheDocument()
  })

  it('explains blockers and preserves their review and removal controls', () => {
    h.leaves = [leaf(1, 'open', { tier: 'blocked', blockedBy: ['o/r#2'], url: 'https://github.com/o/r/issues/1' })]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    expect(screen.getByText('Resolve the blocking issues below, or remove a dependency if it no longer applies.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '#2' })).toHaveAttribute('href', 'https://github.com/o/r/issues/2')
    expect(screen.getByRole('button', { name: 'Remove blocker o/r#2' })).toBeInTheDocument()
  })

  it('filters attention to open needs-you and blocked issues', () => {
    h.leaves = [
      leaf(1, 'open', { tier: 'needs-you' }), leaf(2, 'open', { tier: 'blocked' }),
      leaf(3, 'open'), leaf(4, 'closed', { tier: 'needs-you' }),
    ]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} attentionOnly /></div></IssueDispositionProvider>)
    expect(screen.getByText('Open · 2')).toBeInTheDocument()
    expect(screen.getByText('#1 Issue 1')).toBeInTheDocument()
    expect(screen.getByText('#2 Issue 2')).toBeInTheDocument()
    expect(screen.queryByText('#3 Issue 3')).not.toBeInTheDocument()
    expect(screen.queryByText('#4 Issue 4')).not.toBeInTheDocument()
  })

  it('explains an empty attention list independently of ordinary open issues', () => {
    h.leaves = [leaf(1, 'open')]
    render(<LeafList scope={{ capability: 'f1' }} product={null} attentionOnly />)
    expect(screen.getByText('No issues need your attention')).toBeInTheDocument()
  })

  it('does not offer attention guidance or blocker removal on closed issues', () => {
    h.leaves = [leaf(1, 'closed', { tier: 'needs-you', reason: 'needs_spec', blockedBy: ['o/r#2'], url: 'https://github.com/o/r/issues/1' })]
    render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
    expect(screen.queryByRole('button', { name: 'Review and decide' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Clarify the requirements/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove blocker o/r#2' })).not.toBeInTheDocument()
  })

})

it('explains bounced and human-wait issues with secondary GitHub links', () => {
  h.leaves = [
    leaf(1, 'open', { reason: 'needs_spec', bounces: 2, tier: 'needs-you', url: 'https://github.com/o/r/issues/1' }),
    leaf(2, 'open', { tier: 'needs-you', sessionStatus: 'awaiting_human', url: 'https://github.com/o/r/issues/2' }),
  ]
  render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
  expect(screen.getByText(/Update the issue description/)).toBeInTheDocument()
  expect(screen.getByText(/Read the issue/)).toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: 'View on GitHub' }).map((a) => a.getAttribute('href'))).toEqual(['https://github.com/o/r/issues/1', 'https://github.com/o/r/issues/2'])
})

it.each(['active', 'in_progress'])('does not describe an %s session as waiting when triage needs input', (sessionStatus) => {
  h.leaves = [leaf(1, 'open', { tier: 'needs-you', triageState: 'needs_human', sessionStatus })]
  render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
  expect(screen.getByRole('link', { name: 'View sessions' })).toHaveAttribute('href', '/sessions')
  expect(screen.queryByRole('link', { name: /waiting session/ })).toBeNull()
})

it.each(['#1 Issue 1', 'Review and decide'])('opens an in-app review from %s even without a GitHub URL', (name) => {
  h.leaves = [leaf(1, 'open', { tier: 'needs-you' })]
  render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Review w1')
})

it('does not offer issue mutations for a pull request leaf', () => {
  h.leaves = [leaf(1, 'open', { kind: 'pr', url: 'https://github.com/o/r/pull/1' })]
  render(<IssueDispositionProvider><div data-page-scroll=""><LeafList scope={{ capability: 'f1' }} product={null} /></div></IssueDispositionProvider>)
  expect(screen.getByRole('link', { name: '#1 Issue 1' })).toHaveAttribute('href', 'https://github.com/o/r/pull/1')
  expect(screen.queryByRole('button', { name: 'Review and decide' })).toBeNull()
})
