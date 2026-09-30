/**
 * One Inbox row: the confidence tier, the proposed-capability chip, and — the
 * reason this file exists — the "auto-accepted · undo" chip a fresh PM Triage
 * loop auto-accept carries for the undo window (spec §8/§9). `InboxScopeBody`
 * already covers the row in its list context (grouping, cursor, keys); this
 * file is the row on its own.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { DecisionProposal, TriageProposal } from '@/api/triage'
import { ProposalCard } from './ProposalCard'
import { ViewportProvider } from '@/shell/useViewport'

const undo = vi.fn()
vi.mock('@/api/hooks', () => ({
  useUndoProposal: () => ({ mutate: undo, isPending: false }),
}))

const p = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  proposalKind: 'triage',
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending',
  createdAt: null, classifiedBy: null, resolvedAt: null, ...over,
})

const d = (over: Partial<DecisionProposal> & { id: string }): DecisionProposal => ({
  proposalKind: 'decision', workItemId: `w-${over.id}`, projectSlug: 'viberpit',
  repo: 'steiner385/viberpit', number: 12, ref: 'steiner385/viberpit#12',
  title: 'Queue or stream?', url: null,
  options: [
    { key: 'queue', label: 'Durable queue', detail: 'Survives a restart.' },
    { key: 'stream', label: 'In-process stream', detail: null },
  ],
  recommendedKey: 'queue', rationale: 'Both work; one costs a table.', chosenKey: null,
  status: 'pending', proposedBy: 'session:s1', resolvedBy: null,
  createdAt: null, resolvedAt: null, ...over,
})

function mount(over: Partial<TriageProposal> & { id?: string } = {}) {
  const onOpen = vi.fn()
  const onToggleSelect = vi.fn()
  render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <ul>
          <ProposalCard
            proposal={p({ id: 'p1', ...over })}
            selected={false}
            checked={false}
            cursor={false}
            onOpen={onOpen}
            onToggleSelect={onToggleSelect}
          />
        </ul>
      </MemoryRouter>
    </ViewportProvider>,
  )
  return { onOpen, onToggleSelect }
}

describe('ProposalCard', () => {
  beforeEach(() => { undo.mockReset() })

  it('shows the auto-accepted chip with an Undo control for a fresh auto-accept', () => {
    mount({ status: 'accepted', classifiedBy: 'auto', resolvedAt: new Date().toISOString() })
    expect(screen.getByText('auto-accepted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /undo auto-accept/i }))
    expect(undo).toHaveBeenCalledWith('p1')
  })

  it('shows no chip on a pending proposal', () => {
    mount({ status: 'pending', classifiedBy: null, resolvedAt: null })
    expect(screen.queryByText('auto-accepted')).toBeNull()
  })

  it('shows no chip on a HUMAN-accepted proposal', () => {
    // `accepted` alone is not the chip's condition — `classifiedBy` is the
    // other half. A person's own accept must never come back at them as
    // "auto-accepted", with an Undo framed as undoing the machine.
    mount({ status: 'accepted', classifiedBy: 'human',
            resolvedAt: new Date().toISOString() })
    expect(screen.queryByText('auto-accepted')).toBeNull()
    expect(screen.queryByRole('button', { name: /undo auto-accept/i })).toBeNull()
  })

  it('disables the select tick on a non-pending row', () => {
    mount({ status: 'accepted', classifiedBy: 'auto', resolvedAt: new Date().toISOString() })
    expect(screen.getByRole('button', { name: 'Select p1' })).toBeDisabled()
  })

  it('leaves the select tick enabled on a pending row', () => {
    mount({ status: 'pending' })
    expect(screen.getByRole('button', { name: 'Select p1' })).toBeEnabled()
  })
})

/** The second card kind (spec 2026-09-18 §6), in the same list, through the same
 *  `CardFrame` — a row whose only action is to open the choice. */
describe('ProposalCard — a decision proposal', () => {
  function mountDecision(over: Partial<DecisionProposal> = {}) {
    const onOpen = vi.fn()
    render(
      <ViewportProvider kiosk={null}>
        <MemoryRouter>
          <ul>
            <ProposalCard
              proposal={d({ id: 'd1', ...over })}
              selected={false}
              checked={false}
              cursor={false}
              onOpen={onOpen}
              onToggleSelect={vi.fn()}
            />
          </ul>
        </MemoryRouter>
      </ViewportProvider>,
    )
    return { onOpen }
  }

  it('names the question, the ref, the option count and the recommendation', () => {
    mountDecision()
    expect(screen.getByText('Queue or stream?')).toBeInTheDocument()
    expect(screen.getByText('2 options')).toBeInTheDocument()
    expect(screen.getByText(/steiner385\/viberpit#12/)).toBeInTheDocument()
    expect(screen.getByText(/recommended: Durable queue/)).toBeInTheDocument()
  })

  it('a decision row shows how much it blocks, and nothing when it blocks nothing', () => {
    mountDecision({ downstream: 7 })
    expect(screen.getByText('Blocks 7')).toBeInTheDocument()
    cleanup()
    mountDecision({ downstream: 0 })
    expect(screen.queryByText(/Blocks/)).toBeNull()
    cleanup()
    mountDecision()
    expect(screen.queryByText(/Blocks/)).toBeNull()
  })

  it('says what it is in words, not only in the rail colour', () => {
    mountDecision()
    expect(screen.getByText('awaiting your decision')).toBeInTheDocument()
  })

  it('offers NO select tick — a decision cannot go into a bulk accept', () => {
    mountDecision()
    expect(screen.queryByRole('button', { name: /^Select / })).toBeNull()
  })

  it('opens the pane, which is where the options are', () => {
    const { onOpen } = mountDecision()
    fireEvent.click(screen.getByRole('button', { name: /Open decision Queue or stream\?/ }))
    expect(onOpen).toHaveBeenCalled()
  })
})
