import { describe, it, expect } from 'vitest'
import type { TriageProposal } from '@/api/triage'
import { pendingProposals } from './triageSelectors'

function proposal(over: Partial<TriageProposal> = {}): TriageProposal {
  return {
    id: 'p1', workItemId: 'w1', projectSlug: 'viberpit', repo: 'steiner385/viberpit',
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
})
