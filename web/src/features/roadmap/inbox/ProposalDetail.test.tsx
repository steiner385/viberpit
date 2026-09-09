// The proposal editor as the Inbox scope's DETAIL PANE (roadmap spec §12): the
// old page's inline form, re-homed beside the queue it decides. What is asserted
// here is the decision surface — the scores, the computed RICE, the capability
// the accept will home the item on, and that every action ends with `onDone` so
// the page can move to the next proposal.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ComponentProps } from 'react'
import type { TriageProposal } from '@/api/triage'
import { ProposalDetail } from './ProposalDetail'

const accept = vi.fn()
const reject = vi.fn()
const overlay = vi.fn().mockResolvedValue({})
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: accept, isPending: false }),
  useRejectProposal: () => ({ mutate: reject, isPending: false }),
  useUpdateOverlay: () => ({ mutateAsync: overlay, isPending: false }),
}))

const proposal: TriageProposal = {
  id: 'p1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7, kind: 'issue',
  title: 'Bulk edit', url: 'https://x/7', proposedFeatureId: 'f-tasks', proposedFeatureSlug: 'tasks',
  noFitSuggestion: null, reach: 3, impact: 3, effort: 2, confidence: 80,
  rationale: 'Touches the task list.', confidenceLevel: 'high', status: 'pending',
  createdAt: '2026-09-08T10:00:00Z',
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
    overlay.mockClear()
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
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '' } })
    // Nothing to divide by: the read-out says "not scored", not a stale 360.
    expect(screen.getByText('—')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('effort'), { target: { value: '4' } })
    expect(screen.getByText('180')).toBeInTheDocument()
  })

  it('Accept accepts as proposed and calls onDone', () => {
    const { onDone } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1' })
    expect(onDone).toHaveBeenCalled()
  })

  it('Save edits & accept patches the overlay then accepts with the edits', async () => {
    const { onDone } = mount()
    fireEvent.change(screen.getByLabelText('impact'), { target: { value: '9' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: 'f-billing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    await waitFor(() => expect(overlay).toHaveBeenCalledWith({
      itemId: 'w1', edit: expect.objectContaining({ impact: 9, featureId: 'f-billing' }),
    }))
    expect(accept).toHaveBeenCalledWith({
      id: 'p1', edits: expect.objectContaining({ impact: 9, featureId: 'f-billing' }),
    })
    expect(onDone).toHaveBeenCalled()
  })

  it('leaves Save edits & accept disabled until something actually changes', () => {
    mount()
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('reach'), { target: { value: '5' } })
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeEnabled()
  })

  it('accepts with the edits even when there is no overlay to patch', async () => {
    const { onDone } = mount({ proposal: { ...proposal, workItemId: null } })
    fireEvent.change(screen.getByLabelText('reach'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    // No mirrored work item means no overlay row; the accept body carries the
    // same edits, so the decision must not be dropped on the floor.
    await waitFor(() => expect(accept).toHaveBeenCalledWith({
      id: 'p1', edits: expect.objectContaining({ reach: 5 }),
    }))
    expect(overlay).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalled()
  })

  it('keeps the pane open when the overlay patch fails', async () => {
    overlay.mockRejectedValueOnce(new Error('nope'))
    const { onDone } = mount()
    fireEvent.change(screen.getByLabelText('reach'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    await waitFor(() => expect(overlay).toHaveBeenCalled())
    // The hook toasts the failure; accepting anyway would land scores the
    // server just refused, and moving on would hide that it did.
    expect(accept).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
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
