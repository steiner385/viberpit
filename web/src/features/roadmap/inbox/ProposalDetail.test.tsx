// The proposal editor as the Inbox scope's DETAIL PANE (roadmap spec §12): the
// old page's inline form, re-homed beside the queue it decides. What is asserted
// here is the decision surface — the scores, the computed RICE, the capability
// the accept will home the item on, and that every action ends with `onDone` so
// the page can move to the next proposal.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ComponentProps } from 'react'
import type { TriageProposal } from '@/api/triage'
import { ProposalDetail } from './ProposalDetail'

const accept = vi.fn()
const reject = vi.fn()
// The SCOPE each mutation was keyed to — the cache entry its optimistic
// removal will patch.
const acceptScope = vi.fn()
const rejectScope = vi.fn()
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: (slug?: string) => { acceptScope(slug); return { mutate: accept, isPending: false } },
  useRejectProposal: (slug?: string) => { rejectScope(slug); return { mutate: reject, isPending: false } },
}))

const proposal: TriageProposal = {
  id: 'p1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7, kind: 'issue',
  title: 'Bulk edit', url: 'https://x/7', proposedFeatureId: 'f-tasks', proposedFeatureSlug: 'tasks',
  noFitSuggestion: null, reach: 3, impact: 3, effort: 2, confidence: 80,
  rationale: 'Touches the task list.', confidenceLevel: 'high', status: 'pending',
  createdAt: '2026-09-08T10:00:00Z', classifiedBy: null, resolvedAt: null,
}
const caps = [
  { id: 'f-tasks', slug: 'tasks', title: 'Tasks', depth: 0 },
  { id: 'f-billing', slug: 'billing', title: 'Billing', depth: 0 },
]

function mount(over: Partial<ComponentProps<typeof ProposalDetail>> = {}) {
  const onDone = vi.fn()
  render(
    <MemoryRouter>
      <ProposalDetail
        proposal={proposal}
        capabilities={caps}
        scopeProduct={null}
        autoFocusScores={false}
        onDone={onDone}
        {...over}
      />
    </MemoryRouter>,
  )
  return { onDone }
}

describe('ProposalDetail', () => {
  beforeEach(() => {
    accept.mockReset()
    reject.mockReset()
    acceptScope.mockClear()
    rejectScope.mockClear()
  })

  it('shows the proposal, scores and computed RICE', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Bulk edit' })).toHaveAttribute('href', 'https://x/7')
    expect(screen.getByText('cairnea/KinDash#7')).toBeInTheDocument()
    expect(screen.getByText('Touches the task list.')).toBeInTheDocument()
    expect(screen.getByLabelText('reach')).toHaveValue(3)
    expect(screen.getByLabelText('effort')).toHaveValue(2)
    expect(screen.getByText('360')).toBeInTheDocument()
    expect(screen.getByText('high confidence')).toBeInTheDocument()
  })

  it('recomputes the score as the fields are edited', () => {
    mount()
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '4' } })
    expect(screen.getByText('180')).toBeInTheDocument()
  })

  it('says "not scored" when the proposal itself has no effort', () => {
    mount({ proposal: { ...proposal, effort: null } })
    // Nothing to divide by, and nothing to fall back to either.
    expect(screen.getByLabelText('effort')).toHaveValue(null)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('Accept accepts as proposed and calls onDone', () => {
    const { onDone } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1' })
    expect(onDone).toHaveBeenCalled()
  })

  it('Save edits & accept sends only what changed, in one call', () => {
    const { onDone } = mount()
    fireEvent.change(screen.getByLabelText('impact'), { target: { value: '9' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: 'f-billing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    // No overlay PATCH first: `apply_accept` snapshots the overlay for Undo as
    // it finds it, so a pre-accept write would put the edits INTO the snapshot
    // and undo would restore the decision. The accept applies the same fields.
    expect(accept).toHaveBeenCalledWith({ id: 'p1', edits: { impact: 9, featureId: 'f-billing' } })
    expect(onDone).toHaveBeenCalled()
  })

  it('leaves Save edits & accept disabled until something actually changes', () => {
    mount()
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('reach'), { target: { value: '5' } })
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeEnabled()
  })

  it('keeps the proposal\'s value for a field the user cleared', () => {
    mount()
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '' } })
    // An empty box is the ABSENCE of an override, not "unset this": the accept
    // would keep the proposal's effort either way, so the read-out has to say
    // so rather than showing an em dash the decision does not honour.
    expect(screen.getByText('360')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeDisabled()
  })

  it('keys the mutations to the scope the list is observing, not the proposal', () => {
    mount()
    // Unscoped inbox: the rows came from `[...QK.triage, null]`, so an accept
    // keyed on the proposal's own product would optimistically remove the row
    // from a cache entry nobody is reading.
    expect(acceptScope).toHaveBeenCalledWith(undefined)
    expect(rejectScope).toHaveBeenCalledWith(undefined)
  })

  it('keys them to the product when the inbox is narrowed', () => {
    mount({ scopeProduct: 'kindash' })
    expect(acceptScope).toHaveBeenCalledWith('kindash')
    expect(rejectScope).toHaveBeenCalledWith('kindash')
  })

  it('Reject rejects and calls onDone', () => {
    const { onDone } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(reject).toHaveBeenCalledWith({ id: 'p1' })
    expect(onDone).toHaveBeenCalled()
  })

  it('autoFocusScores focuses the first score field', () => {
    mount({ autoFocusScores: true })
    expect(screen.getByLabelText('reach')).toHaveFocus()
  })

  it('names the no-fit suggestion and offers "no fit" as the default option', () => {
    mount({
      proposal: {
        ...proposal, proposedFeatureId: null, proposedFeatureSlug: null, noFitSuggestion: 'exports',
      },
    })
    expect(screen.getByText(/no fit → exports/)).toBeInTheDocument()
    const select = screen.getByRole('combobox', { name: 'Capability' })
    expect(select).toHaveValue('')
    expect((select as HTMLSelectElement).options[0]!.textContent).toBe('— no fit —')
  })

  it('swallows the inbox letter keys so a focused button cannot accept the row behind it', () => {
    const seen = vi.fn()
    window.addEventListener('keydown', seen)
    try {
      mount()
      const button = screen.getByRole('button', { name: 'Accept' })
      button.focus()
      // A button is not a typing target, so the list's window-level key map
      // would otherwise act on ITS cursor row — a different proposal than the
      // one on screen, accepted irreversibly.
      fireEvent.keyDown(button, { key: 'a' })
      fireEvent.keyDown(button, { key: 'r' })
      expect(seen).not.toHaveBeenCalled()
      // Everything else still belongs to the app (Escape, Tab, shortcuts).
      fireEvent.keyDown(button, { key: 'Escape' })
      expect(seen).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('keydown', seen)
    }
  })
})
