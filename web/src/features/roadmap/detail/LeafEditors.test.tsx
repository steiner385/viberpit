// The two inline edits a leaf row offers (spec §8): the score, through the same
// four fields the inbox uses, and the capability it sits under.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Leaf } from '@/api/roadmap'
import { LeafScoreButton, LeafCapabilityChip } from './LeafEditors'

const h = vi.hoisted(() => ({ mutate: vi.fn() }))
vi.mock('@/api/hooks', () => ({
  useUpdateOverlay: () => ({ mutate: h.mutate, isPending: false, isError: false, error: null }),
}))

const leaf: Leaf = {
  id: 'w1', ref: 'o/r#7', repo: 'o/r', number: 7, kind: 'issue', title: 'Add filters', url: null,
  state: 'open', triageState: 'classified', score: 12.5, reach: 5, impact: 5, effort: 2, confidence: 1,
  tier: 'idle', blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: 'f1',
}
const node = (id: string, slug: string, title: string) => ({
  id, slug, title, state: 'live', summary: null, docPath: null, commitSha: null, prNumber: null,
  parentSlug: null, tier: 'idle' as const, counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
  links: [], initiatives: [], children: [],
})
const capabilities = [{ node: node('f1', 'task-views', 'Task views'), depth: 0 }, { node: node('f2', 'filters', 'Filters'), depth: 1 }]

describe('LeafScoreButton', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('shows the score, opens the four fields seeded, and sends only what changed', () => {
    render(<LeafScoreButton leaf={leaf} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit score for #7' }))
    expect(screen.getByLabelText('reach')).toHaveValue(5)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '4' } })
    expect(screen.getByText('6.25')).toBeInTheDocument()     // live read-out: 5*5*1/4
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.mutate).toHaveBeenCalledWith({ itemId: 'w1', edit: { effort: 4 } }, expect.anything())
  })

  it('an unscored leaf opens blank and reads "—"', () => {
    render(<LeafScoreButton leaf={{ ...leaf, score: null, reach: null, impact: null, effort: null, confidence: null }} />)
    expect(screen.getByRole('button', { name: 'Edit score for #7' })).toHaveTextContent('—')
  })
})

describe('LeafCapabilityChip', () => {
  beforeEach(() => { h.mutate.mockReset() })

  it('names the current capability and moves the leaf on Save', () => {
    render(<LeafCapabilityChip leaf={leaf} capabilities={capabilities} />)
    const chip = screen.getByRole('button', { name: 'Move #7 to another capability' })
    expect(chip).toHaveTextContent('Task views')
    fireEvent.click(chip)
    const select = screen.getByLabelText('Capability')
    expect(select).toHaveValue('f1')
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
    fireEvent.change(select, { target: { value: 'f2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move' }))
    expect(h.mutate).toHaveBeenCalledWith({ itemId: 'w1', edit: { featureId: 'f2' } }, expect.anything())
  })

  it('renders nothing to click when the tree is not known', () => {
    render(<LeafCapabilityChip leaf={leaf} capabilities={[]} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
