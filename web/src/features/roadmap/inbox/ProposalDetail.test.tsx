// The proposal editor as the Inbox scope's DETAIL PANE (roadmap spec §12): the
// old page's inline form, re-homed beside the queue it decides. What is asserted
// here is the decision surface — the scores, the computed RICE, the capability
// the accept will home the item on, and that every action ends with `onDone` so
// the page can move to the next proposal.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ComponentProps } from 'react'
import type { DecisionProposal, TriageProposal } from '@/api/triage'
import { ProposalDetail } from './ProposalDetail'

const accept = vi.fn()
const reject = vi.fn()
// The SCOPE each mutation was keyed to — the cache entry its optimistic
// removal will patch.
const acceptScope = vi.fn()
const rejectScope = vi.fn()
const acceptDecision = vi.fn()
const rejectDecision = vi.fn()
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: (slug?: string) => { acceptScope(slug); return { mutate: accept, isPending: false } },
  useRejectProposal: (slug?: string) => { rejectScope(slug); return { mutate: reject, isPending: false } },
  useAcceptDecision: () => ({ mutate: acceptDecision, isPending: false }),
  useRejectDecision: () => ({ mutate: rejectDecision, isPending: false }),
}))

const proposal: TriageProposal = {
  proposalKind: 'triage', id: 'p1', workItemId: 'w1', projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 7, kind: 'issue',
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

  it('accepting with a newly typed capability sends newCapability instead of featureId', () => {
    const { onDone } = mount()
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: '__new__' } })
    fireEvent.change(screen.getByLabelText(/new capability title/i), { target: { value: 'Payments' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1', edits: { newCapability: { title: 'Payments', slug: 'payments' } } })
    expect(onDone).toHaveBeenCalled()
  })

  it('derives the slug from the typed title until the slug field is edited by hand', () => {
    mount()
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: '__new__' } })
    fireEvent.change(screen.getByLabelText(/new capability title/i), { target: { value: 'Payments & Billing' } })
    expect(screen.getByLabelText('Slug')).toHaveValue('payments-billing')
    fireEvent.change(screen.getByLabelText('Slug'), { target: { value: 'custom-slug' } })
    // Taken over: further typing in the title no longer overwrites it.
    fireEvent.change(screen.getByLabelText(/new capability title/i), { target: { value: 'Payments & Billing 2' } })
    expect(screen.getByLabelText('Slug')).toHaveValue('custom-slug')
  })

  it('leaves Save edits & accept disabled while the new-capability title is still empty', () => {
    mount()
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: '__new__' } })
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/new capability title/i), { target: { value: 'Payments' } })
    expect(screen.getByRole('button', { name: 'Save edits & accept' })).toBeEnabled()
  })

  it('switching back to an existing capability drops the new-capability draft', () => {
    const { onDone } = mount()
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: '__new__' } })
    fireEvent.change(screen.getByLabelText(/new capability title/i), { target: { value: 'Payments' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Capability' }), { target: { value: 'f-billing' } })
    expect(screen.queryByLabelText(/new capability title/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save edits & accept' }))
    expect(accept).toHaveBeenCalledWith({ id: 'p1', edits: { featureId: 'f-billing' } })
    expect(onDone).toHaveBeenCalled()
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

  it('says the capabilities are still loading rather than looking empty (#1496)', () => {
    // The options come from `/pm/map`, a different query from the one that
    // delivered this proposal, so the editor can render complete with an empty
    // select. Undifferentiated that reads as "this product has no capabilities",
    // and accepting on that belief homes the issue nowhere.
    mount({ capabilities: [], capabilitiesLoading: true })
    const select = screen.getByRole('combobox', { name: 'Capability' }) as HTMLSelectElement
    const loading = [...select.options].find((o) => o.textContent === 'Loading capabilities…')
    expect(loading).toBeDefined()
    // Never selectable: it is a sentence, not a home for the issue.
    expect(loading!.disabled).toBe(true)
  })

  it('says nothing about loading once the map has answered', () => {
    // A product that genuinely has no capability yet gets the honest empty
    // select — the note is about the QUERY, not about the count.
    mount({ capabilities: [] })
    const select = screen.getByRole('combobox', { name: 'Capability' }) as HTMLSelectElement
    expect([...select.options].map((o) => o.textContent)).toEqual(['— keep proposal —', '+ New capability'])
  })

  it('says what Accept and Reject do under the product policy', () => {
    mount({ proposal: { ...proposal, guidance: {
      accept: { available: true, unavailableReason: null,
        immediate: [{ text: 'Files it under tasks with score 360; it leaves the inbox.', spawnsSession: false, writesGithub: false }],
        then: [{ text: "The Backlog Runner may dispatch it to a coding session once it is at or above 'ready' on the board.", spawnsSession: true, writesGithub: false }],
        when: 'next_tick', reversal: 'Undo within the window', gate: 'triage.accept' },
      reject: { available: true, unavailableReason: null,
        immediate: [{ text: 'Marks it needs-human; it leaves the inbox and nothing is dispatched.', spawnsSession: false, writesGithub: false }],
        then: [], when: 'now', reversal: null, gate: null },
    } } })
    const region = screen.getByRole('group', { name: 'What each decision does' })
    expect(region).toHaveTextContent('Files it under tasks with score 360')
    expect(region).toHaveTextContent('starts a session')
    expect(region).toHaveTextContent('Then · on the next automation run')
    expect(region).toHaveTextContent('Reverse: Undo within the window')
    expect(region).toHaveTextContent('Marks it needs-human')
  })

  it('renders no guidance block when the server sends none', () => {
    mount()
    expect(screen.queryByRole('group', { name: 'What each decision does' })).not.toBeInTheDocument()
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

/**
 * The DECISION pane (spec 2026-09-18 §6): a single-choice group of the options a
 * session drafted, the recommendation preselected, the rationale above it, and
 * the same two-column guidance the classification pane renders.
 */
const GUIDANCE = {
  accept: {
    available: true, unavailableReason: null,
    immediate: [{ text: 'The hold clears; the next run implements with the chosen option.', spawnsSession: false, writesGithub: false }],
    then: [{ text: 'The Backlog Runner may dispatch it.', spawnsSession: true, writesGithub: false }],
    when: 'next_tick' as const, reversal: 'Park the item again', gate: null,
  },
  reject: {
    available: true, unavailableReason: null,
    immediate: [{ text: "The issue is held for you with 'decision proposal rejected'.", spawnsSession: false, writesGithub: false }],
    then: [], when: 'now' as const, reversal: null, gate: null,
  },
}

const decision: DecisionProposal = {
  proposalKind: 'decision', id: 'd1', workItemId: 'w9', repo: 'steiner385/viberpit', number: 12,
  ref: 'steiner385/viberpit#12', projectSlug: 'viberpit', title: 'Queue or stream?', url: null,
  options: [
    { key: 'queue', label: 'Durable queue', detail: 'Survives a restart; costs a table.' },
    { key: 'stream', label: 'In-process stream', detail: null },
  ],
  recommendedKey: 'queue', rationale: 'Both work; one costs a table.', chosenKey: null,
  status: 'pending', proposedBy: 'session:s1', resolvedBy: null,
  createdAt: '2026-09-18T10:00:00Z', resolvedAt: null,
}

function mountDecision(over: Partial<DecisionProposal> = {}) {
  const onDone = vi.fn()
  render(
    <MemoryRouter>
      <ProposalDetail
        proposal={{ ...decision, ...over }}
        capabilities={caps}
        scopeProduct={null}
        autoFocusScores={false}
        onDone={onDone}
      />
    </MemoryRouter>,
  )
  return { onDone }
}

describe('ProposalDetail — a decision proposal', () => {
  beforeEach(() => {
    acceptDecision.mockReset()
    rejectDecision.mockReset()
  })

  it('offers the options as ONE single-choice group, with the recommendation preselected', () => {
    mountDecision()
    const group = screen.getByRole('radiogroup', { name: 'Options' })
    expect(group).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Durable queue/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /In-process stream/ })).not.toBeChecked()
    expect(screen.getByText('recommended')).toBeInTheDocument()
    // The detail under an option is the difference the person is choosing on.
    expect(screen.getByText(/Survives a restart/)).toBeInTheDocument()
  })

  it('reads the rationale and who proposed it before the choice', () => {
    mountDecision()
    expect(screen.getByText('Both work; one costs a table.')).toBeInTheDocument()
    expect(screen.getByText('Proposed by session:s1')).toBeInTheDocument()
  })

  it('accepts the option the person picked, not the recommendation', () => {
    const { onDone } = mountDecision()
    fireEvent.click(screen.getByRole('radio', { name: /In-process stream/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(acceptDecision).toHaveBeenCalledWith({ id: 'd1', chosenKey: 'stream' })
    expect(onDone).toHaveBeenCalled()
  })

  it('accepts the recommendation when nothing is changed', () => {
    mountDecision()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(acceptDecision).toHaveBeenCalledWith({ id: 'd1', chosenKey: 'queue' })
  })

  it('rejects through the decision route, and moves on', () => {
    const { onDone } = mountDecision()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(rejectDecision).toHaveBeenCalledWith({ id: 'd1' })
    expect(accept).not.toHaveBeenCalled()
    expect(reject).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalled()
  })

  it('cannot accept with no option chosen — the server would 422', () => {
    mountDecision({ recommendedKey: null })
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled()
  })

  it('offers no edit-then-accept: the options ARE the whole choice', () => {
    mountDecision()
    expect(screen.queryByRole('button', { name: /Save edits/ })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Capability' })).toBeNull()
  })

  it('says what Accept and Reject do, through the same renderer as the other card', () => {
    mountDecision({ guidance: GUIDANCE })
    const region = screen.getByRole('group', { name: 'What each decision does' })
    expect(region).toHaveTextContent('The hold clears; the next run implements with the chosen option.')
    expect(region).toHaveTextContent('starts a session')
    expect(region).toHaveTextContent("held for you with 'decision proposal rejected'")
  })

  it('swallows the inbox letter keys here too', () => {
    const seen = vi.fn()
    window.addEventListener('keydown', seen)
    try {
      mountDecision()
      const button = screen.getByRole('button', { name: 'Accept' })
      button.focus()
      fireEvent.keyDown(button, { key: 'a' })
      fireEvent.keyDown(button, { key: 'r' })
      expect(seen).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', seen)
    }
  })
})
