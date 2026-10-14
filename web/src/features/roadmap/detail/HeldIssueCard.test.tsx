// The held card is the Roadmap's answer to "what am I supposed to do with this":
// the issue (title + link), why it is held in plain words, and the exact taps that
// release it — each bound to THIS card's work item, never to a neighbour's.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { HeldItem } from '@/api/roadmap'
import { HeldIssueCard } from './HeldIssueCard'
import { IssueDispositionContext } from './IssueDispositionContext'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const h = vi.hoisted(() => ({
  release: vi.fn(), approve: vi.fn(),
  releaseFor: [] as string[], approveFor: [] as string[],
}))
vi.mock('@/api/hooks', () => ({
  useReleaseHold: (id: string) => { h.releaseFor.push(id); return { mutateAsync: () => h.release(id), isPending: false, error: null } },
  useApproveEscalation: (id: string) => { h.approveFor.push(id); return { mutateAsync: (model: string) => h.approve(id, model), isPending: false, error: null } },
}))

const item = (over: Partial<HeldItem> = {}): HeldItem => ({
  ref: 'steiner385/ghostdriver-mcp#28', repo: 'steiner385/ghostdriver-mcp', number: 28, product: 'ghostdriver',
  title: 'Gate 3 scale is unproven: it ran on 108 windows, not ~1,100',
  url: 'https://github.com/steiner385/ghostdriver-mcp/issues/28',
  holdKind: 'owner_action', holdReason: 'bounced at opus; fable needs approval', holdBy: 'runner',
  holdSetAt: '2026-09-20T12:29:56+00:00', ageHours: 290.9, proposalId: null, options: [], recommendedKey: null,
  downstream: 0, downstreamDispatchable: 0, downstreamRefs: [], dispatchableOnRelease: true,
  releasedBy: 'Retry, a board move to Ready/Backlog, an issue-body edit, or approving the escalation to fable',
  workItemId: 'w28', escalation: { bouncedAt: 'opus', needsApproval: 'fable' }, attentionKind: 'model_approval', ...over,
})

beforeEach(() => { h.release.mockReset(); h.approve.mockReset(); h.releaseFor.length = 0; h.approveFor.length = 0 })

function mount(items: HeldItem[], review = vi.fn()) {
  render(<MemoryRouter><IssueDispositionContext.Provider value={review}>
    <ul>{items.map((i) => <HeldIssueCard key={i.ref} item={i} />)}</ul>
  </IssueDispositionContext.Provider></MemoryRouter>)
  return review
}

describe('HeldIssueCard', () => {
  it('names the issue, explains a model-escalation hold in plain words and offers every release tap', () => {
    mount([item()])
    const card = screen.getByTestId('held-issue')
    expect(within(card).getByRole('link', { name: '#28 Gate 3 scale is unproven: it ran on 108 windows, not ~1,100' }))
      .toHaveAttribute('href', 'https://github.com/steiner385/ghostdriver-mcp/issues/28')
    expect(card).toHaveTextContent('Model approval')
    expect(card).toHaveTextContent('held 12 days')
    expect(card).toHaveTextContent(/The Backlog Runner could not get a pull request merged at Opus and stopped: the next step up is Fable, which your model-approval policy gates behind a person\./)
    expect(card).toHaveTextContent(/Approve escalation to run it once at Fable now, or Retry/)
    expect(within(card).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Approve escalation to Fable' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Review and decide' })).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: 'Edit on GitHub' })).toHaveAttribute('href', 'https://github.com/steiner385/ghostdriver-mcp/issues/28')
    // The raw runner string is not the explanation — it is translated, not echoed.
    expect(card).not.toHaveTextContent('bounced at opus; fable needs approval')
  })

  it('binds every tap to its own work item, never a neighbour', async () => {
    h.release.mockResolvedValue({ ok: true }); h.approve.mockResolvedValue({ status: 'spawned', session_id: 's-1', session_name: 'backlog #28', approval: 'approved', model: 'fable' })
    const review = mount([
      item({ ref: 'o/r#1', number: 1, title: 'First', workItemId: 'w1', escalation: null, holdReason: 'needs the keyring', attentionKind: 'owner_action' }),
      item(),
    ])
    const [first, second] = screen.getAllByTestId('held-issue')
    fireEvent.click(within(first).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(h.release).toHaveBeenCalledWith('w1'))
    fireEvent.click(within(second).getByRole('button', { name: 'Approve escalation to Fable' }))
    await waitFor(() => expect(h.approve).toHaveBeenCalledWith('w28', 'fable'))
    fireEvent.click(within(second).getByRole('button', { name: 'Review and decide' }))
    expect(review).toHaveBeenCalledWith('w28')
    expect(within(first).queryByRole('button', { name: /Approve escalation/ })).toBeNull()
    expect(first).toHaveTextContent('The Backlog Runner handed this to you: needs the keyring.')
    expect(await within(second).findByRole('link', { name: 'Open session backlog #28' })).toHaveAttribute('href', expect.stringContaining('s-1'))
  })

  it('says what happened when the dispatch refused or the session is parked for approval', async () => {
    h.approve.mockResolvedValueOnce({ status: 'no_account', session_id: null, session_name: null, approval: 'not_needed', model: 'fable' })
      .mockResolvedValueOnce({ status: 'spawned', session_id: 's-2', session_name: null, approval: 'pending', model: 'fable' })
    mount([item(), item({ ref: 'o/r#2', number: 2, workItemId: 'w2' })])
    const [a, b] = screen.getAllByTestId('held-issue')
    fireEvent.click(within(a).getByRole('button', { name: 'Approve escalation to Fable' }))
    expect(await within(a).findByRole('status')).toHaveTextContent("Dispatch failed: no_account. The hold was released; the next run uses the product's default model.")
    fireEvent.click(within(b).getByRole('button', { name: 'Approve escalation to Fable' }))
    expect(await within(b).findByRole('status')).toHaveTextContent('parked for approval at Fable — approve it on the Sessions page')
  })

  it('points a decision hold at its proposal and offers no Retry', () => {
    mount([item({ holdKind: 'decision', holdReason: 'Awaiting a product decision', holdBy: 'session:s1', escalation: null, proposalId: 'dp-9', attentionKind: 'decision' })])
    const card = screen.getByTestId('held-issue')
    expect(card).toHaveTextContent('Awaiting your decision')
    expect(card).toHaveTextContent(/A session proposed options instead of guessing/)
    expect(within(card).queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(within(card).getByRole('link', { name: 'Open in the inbox' })).toHaveAttribute('href', '/roadmap?scope=inbox&proposal=dp-9')
  })

  it('labels each kind of ask and renders an assessed checklist as a list', () => {
    mount([
      item({ ref: 'o/r#1', number: 1, workItemId: 'w1', escalation: null, attentionKind: 'owner_action', holdReason: 'needs the keyring' }),
      item({
        ref: 'o/r#2', number: 2, workItemId: 'w2', escalation: null, attentionKind: 'checklist',
        holdBy: 'automation:needs-you-hygiene',
        holdReason: 'Left for you:\n- [ ] Verify the fix on production\n- [ ] Renew the Apple certificate\n(+3 more)',
      }),
    ])
    const [plain, checklist] = screen.getAllByTestId('held-issue')
    expect(plain).toHaveTextContent('Held for the owner')
    expect(checklist).toHaveTextContent('Checklist for you')
    expect(checklist).toHaveTextContent('The needs-you-hygiene automation checked what the merged pull request left open. Only you can do the rest:')
    const list = within(checklist).getByRole('list', { name: 'Left for you' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Verify the fix on production', 'Renew the Apple certificate'])
    expect(checklist).toHaveTextContent('(+3 more)')
    expect(checklist).not.toHaveTextContent('- [ ]')
    expect(within(checklist).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('renders only the GitHub link when the mirror has no work-item id', () => {
    mount([item({ workItemId: null })])
    const card = screen.getByTestId('held-issue')
    expect(within(card).queryByRole('button')).toBeNull()
    expect(within(card).getByRole('link', { name: 'Edit on GitHub' })).toBeInTheDocument()
  })
})
