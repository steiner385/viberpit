// Today's digest (design 2026-10-14 §5): every needs-you item with its ONE
// recommended action, and "Approve all" behind a dialog that names each action.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { DigestItem, OwnerDigest } from '@/api/roadmap'
import { product } from '../__fixtures__/map'
import { OwnerDigestCard } from './OwnerDigestCard'

const h = vi.hoisted(() => ({
  digest: undefined as OwnerDigest | undefined,
  digestFor: [] as (string | undefined)[],
  apply: vi.fn(),
}))
vi.mock('@/api/hooks', () => ({
  useOwnerDigest: (p: string | undefined) => { h.digestFor.push(p); return { data: h.digest } },
  useApplyDigest: () => ({ mutateAsync: (ids: string[]) => h.apply(ids), isPending: false, error: null, variables: undefined }),
}))

const item = (over: Partial<DigestItem>): DigestItem => ({
  id: 'x', kind: 'issue', product: 'kindash', ref: null, slug: null, title: 't', why: '', url: null,
  recommendation: null, evidence: [], ...over,
})

const ITEMS: DigestItem[] = [
  item({ id: 'hold:w1', kind: 'model_approval', ref: 'o/k#250', title: 'Fix the sync race',
         url: 'https://github.com/o/k/issues/250', why: 'bounced at opus; fable needs approval',
         recommendation: { action: 'approve_escalation', label: 'Approve fable', params: { model: 'fable' } } }),
  item({ id: 'rec:r1', kind: 'recommendation', ref: 'o/k#120', title: 'Export chores as CSV',
         why: 'o/k#120 is parked, but a merged PR already does it',
         recommendation: { action: 'apply_recommendation', label: 'Close', params: {} },
         evidence: [{ title: '#310 feat: CSV export', url: 'https://github.com/o/k/pull/310' }] }),
  item({ id: 'gate:g', kind: 'gate', slug: 'g', title: 'One-page checkout', url: '/initiatives/g',
         why: 'Waiting for your review at its stage gate.' }),
]

beforeEach(() => {
  h.digest = { generatedAt: '', products: [{ slug: 'kindash', items: ITEMS }], total: 3, withRecommendation: 2 }
  h.digestFor.length = 0
  h.apply.mockReset()
  h.apply.mockResolvedValue({ results: [{ id: 'hold:w1', ok: true, step: null, error: null }], applied: 1, failed: 0 })
})

const mount = (slugs = ['kindash']) =>
  render(<MemoryRouter><OwnerDigestCard products={slugs.map((slug) => product({ slug }))} /></MemoryRouter>)

describe('OwnerDigestCard', () => {
  it('lists each item with its kind, why, evidence and one recommended action', () => {
    mount()
    expect(h.digestFor).toContain('kindash')
    expect(screen.getByText('3 items need you — 2 have a recommendation')).toBeInTheDocument()
    const rows = screen.getAllByTestId('digest-item')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByRole('button', { name: 'Approve fable' })).toBeInTheDocument()
    expect(within(rows[1]).getByRole('link', { name: '#310 feat: CSV export' })).toHaveAttribute('href', 'https://github.com/o/k/pull/310')
    // A gate is never advanced from the digest: it links to the gate, no button.
    expect(within(rows[2]).queryByRole('button')).not.toBeInTheDocument()
    expect(within(rows[2]).getByRole('link', { name: 'Review the gate' })).toHaveAttribute('href', '/initiatives/g')
  })

  it('applies one item from its own button', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Approve fable' }))
    await waitFor(() => expect(h.apply).toHaveBeenCalledWith(['hold:w1']))
    expect(await screen.findByRole('status')).toHaveTextContent('Applied 1 of 1.')
  })

  it('approves all only after a dialog that names every action', async () => {
    h.apply.mockResolvedValue({ results: [
      { id: 'hold:w1', ok: true, step: null, error: null },
      { id: 'rec:r1', ok: false, step: 'not_parked', error: 'the issue is no longer parked' }], applied: 1, failed: 1 })
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Approve all recommendations (2)' }))
    expect(h.apply).not.toHaveBeenCalled()
    const list = await screen.findByRole('list', { name: 'What will happen' })
    expect([...list.querySelectorAll('li')].map((li) => li.textContent)).toEqual([
      'Approve fable — Fix the sync race', 'Close — Export chores as CSV'])
    fireEvent.click(screen.getByRole('button', { name: 'Approve all' }))
    await waitFor(() => expect(h.apply).toHaveBeenCalledWith(['hold:w1', 'rec:r1']))
    expect(await screen.findByRole('status')).toHaveTextContent('Not applied: the issue is no longer parked')
  })

  it('reads every product for a portfolio scope and renders nothing when empty', () => {
    h.digest = { generatedAt: '', products: [], total: 0, withRecommendation: 0 }
    const { container } = mount(['kindash', 'viberpit'])
    expect(h.digestFor).toContain(undefined)
    expect(container).toBeEmptyDOMElement()
  })
})
