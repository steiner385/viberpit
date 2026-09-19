import { describe, it, expect } from 'vitest'
import type { DecisionProposal, InboxProposal, TriageProposal } from '@/api/triage'
import { pendingProposals } from './triageSelectors'

function decision(over: Partial<DecisionProposal> = {}): DecisionProposal {
  return {
    proposalKind: 'decision', id: 'd1', workItemId: 'w9', repo: 'steiner385/viberpit',
    number: 9, ref: 'steiner385/viberpit#9', projectSlug: 'viberpit', title: 'Sync or async?',
    url: null, options: [{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }],
    recommendedKey: 'a', rationale: null, chosenKey: null, status: 'pending',
    proposedBy: 'session:s1', resolvedBy: null, createdAt: null, resolvedAt: null,
    ...over,
  }
}

function proposal(over: Partial<TriageProposal> = {}): TriageProposal {
  return {
    proposalKind: 'triage', id: 'p1', workItemId: 'w1', projectSlug: 'viberpit', repo: 'steiner385/viberpit',
    number: 1, kind: 'issue', title: 't', url: null,
    proposedFeatureId: null, proposedFeatureSlug: null, noFitSuggestion: null,
    reach: null, impact: null, effort: null, confidence: null,
    rationale: null, confidenceLevel: null, status: 'pending', createdAt: null,
    classifiedBy: null, resolvedAt: null,
    ...over,
  }
}

describe('pendingProposals', () => {
  it('keeps only the rows still awaiting a decision', () => {
    const rows = [
      proposal({ id: 'a' }),
      proposal({ id: 'auto', status: 'accepted', classifiedBy: 'auto' }),
      proposal({ id: 'b' }),
      proposal({ id: 'human', status: 'accepted', classifiedBy: 'human' }),
      proposal({ id: 'rejected', status: 'rejected' }),
    ]
    expect(pendingProposals(rows).map((p) => p.id)).toEqual(['a', 'b'])
  })

  it('preserves the server order and returns the rows themselves', () => {
    const rows = [proposal({ id: 'z' }), proposal({ id: 'y' })]
    expect(pendingProposals(rows)).toEqual(rows)
    expect(pendingProposals(rows)[0]).toBe(rows[0])
  })

  it('survives an empty queue', () => {
    expect(pendingProposals([])).toEqual([])
  })

  // Spec 2026-09-18 §6: decision proposals list with classification proposals.
  // This is the ONE "awaiting a human" predicate — the nav badge, the scope
  // count and the bulk bar all read it — so it has to answer for both kinds, or
  // the badge says "6 need you" about a queue of 9.
  it('counts a pending decision proposal too, in queue order', () => {
    const rows: InboxProposal[] = [
      proposal({ id: 'a' }),
      decision({ id: 'd-pending' }),
      decision({ id: 'd-accepted', status: 'accepted', chosenKey: 'a' }),
      decision({ id: 'd-rejected', status: 'rejected' }),
    ]
    expect(pendingProposals(rows).map((p) => p.id)).toEqual(['a', 'd-pending'])
  })

  it('keeps each row its own type — a decision comes back a decision', () => {
    const [row] = pendingProposals([decision()])
    expect(row?.proposalKind).toBe('decision')
  })
})
