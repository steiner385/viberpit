/**
 * One Inbox row: the confidence tier, the proposed-capability chip, and — the
 * reason this file exists — the "auto-accepted · undo" chip a fresh PM Triage
 * loop auto-accept carries for the undo window (spec §8/§9). `InboxScopeBody`
 * already covers the row in its list context (grouping, cursor, keys); this
 * file is the row on its own.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import { ProposalCard } from './ProposalCard'
import { ViewportProvider } from '@/shell/useViewport'

const undo = vi.fn()
vi.mock('@/api/hooks', () => ({
  useUndoProposal: () => ({ mutate: undo, isPending: false }),
}))

const p = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending',
  createdAt: null, classifiedBy: null, resolvedAt: null, ...over,
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

  it('disables the select tick on a non-pending row', () => {
    mount({ status: 'accepted', classifiedBy: 'auto', resolvedAt: new Date().toISOString() })
    expect(screen.getByRole('button', { name: 'Select p1' })).toBeDisabled()
  })

  it('leaves the select tick enabled on a pending row', () => {
    mount({ status: 'pending' })
    expect(screen.getByRole('button', { name: 'Select p1' })).toBeEnabled()
  })
})
