import { describe, it, expect } from 'vitest'
import type { DecisionProposal, InboxProposal, TriageProposal } from '@/api/triage'
import {
  INBOX_STALE_AFTER_MS, highConfidenceAcceptable, inboxAging, isItemGone, isPending, pendingProposals,
} from './triageSelectors'

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

describe('isPending — a closed or merged work item is not pending', () => {
  it('drops a pending row whose item closed or merged, keeps open/unknown ones', () => {
    expect(isPending(proposal({ itemState: 'closed' }))).toBe(false)
    expect(isPending(proposal({ itemState: 'merged' }))).toBe(false)
    expect(isPending(proposal({ itemState: 'open' }))).toBe(true)
    // An older server sends no itemState at all.
    expect(isPending(proposal())).toBe(true)
    expect(isPending(proposal({ itemState: null }))).toBe(true)
  })

  it('a dismissed proposal is not pending', () => {
    expect(isPending(proposal({ status: 'dismissed' }))).toBe(false)
  })

  it('isItemGone names exactly the closed/merged rows (decisions never are)', () => {
    expect(isItemGone(proposal({ itemState: 'closed' }))).toBe(true)
    expect(isItemGone(proposal({ itemState: 'merged' }))).toBe(true)
    expect(isItemGone(proposal({ itemState: 'open' }))).toBe(false)
    expect(isItemGone(decision())).toBe(false)
    expect(isItemGone(null)).toBe(false)
  })

  it('the count drops the stale row', () => {
    const rows = [proposal({ id: 'a' }), proposal({ id: 'gone', itemState: 'merged' })]
    expect(pendingProposals(rows).map((p) => p.id)).toEqual(['a'])
  })
})

describe('inboxAging', () => {
  const NOW = Date.parse('2026-10-11T12:00:00Z')
  const daysAgo = (n: number) => new Date(NOW - n * 86_400_000).toISOString()

  it('counts the pending rows of both kinds and finds the oldest', () => {
    const rows: InboxProposal[] = [
      proposal({ id: 'a', createdAt: daysAgo(1) }),
      decision({ id: 'd', createdAt: daysAgo(6) }),
      proposal({ id: 'b', createdAt: daysAgo(3) }),
    ]
    const a = inboxAging(rows, NOW)
    expect(a.pending).toBe(3)
    expect(a.oldestCreatedAt).toBe(daysAgo(6))
    expect(a.oldestAgeMs).toBe(6 * 86_400_000)
    expect(a.stale).toBe(true)
  })

  it('ignores decided, stale-item and timestamp-less rows when picking the oldest', () => {
    const rows = [
      proposal({ id: 'old-accepted', status: 'accepted', createdAt: daysAgo(30) }),
      proposal({ id: 'old-closed', itemState: 'closed', createdAt: daysAgo(20) }),
      proposal({ id: 'no-ts', createdAt: null }),
      proposal({ id: 'fresh', createdAt: daysAgo(1) }),
    ]
    const a = inboxAging(rows, NOW)
    expect(a.pending).toBe(2)
    expect(a.oldestCreatedAt).toBe(daysAgo(1))
    expect(a.stale).toBe(false)
  })

  it('warns only strictly past the two-day threshold', () => {
    const at = (ms: number) => inboxAging([proposal({ createdAt: new Date(NOW - ms).toISOString() })], NOW)
    expect(at(INBOX_STALE_AFTER_MS).stale).toBe(false)
    expect(at(INBOX_STALE_AFTER_MS + 1000).stale).toBe(true)
  })

  it('an empty queue has no oldest and is not stale', () => {
    expect(inboxAging([], NOW)).toEqual({ pending: 0, oldestCreatedAt: null, oldestAgeMs: null, stale: false })
  })

  it('clamps a future createdAt (client clock behind) to age 0', () => {
    const a = inboxAging([proposal({ createdAt: new Date(NOW + 60_000).toISOString() })], NOW)
    expect(a.oldestAgeMs).toBe(0)
  })
})

describe('highConfidenceAcceptable', () => {
  it('takes pending high-confidence classification rows that have a capability', () => {
    const rows: InboxProposal[] = [
      proposal({ id: 'yes', confidenceLevel: 'high', proposedFeatureId: 'f1' }),
      proposal({ id: 'no-fit', confidenceLevel: 'high', proposedFeatureId: null, noFitSuggestion: 'x' }),
      proposal({ id: 'medium', confidenceLevel: 'medium', proposedFeatureId: 'f1' }),
      proposal({ id: 'decided', confidenceLevel: 'high', proposedFeatureId: 'f1', status: 'accepted' }),
      proposal({ id: 'closed', confidenceLevel: 'high', proposedFeatureId: 'f1', itemState: 'closed' }),
      decision({ id: 'decision' }),
      proposal({ id: 'yes2', confidenceLevel: 'high', proposedFeatureId: 'f2' }),
    ]
    expect(highConfidenceAcceptable(rows).map((p) => p.id)).toEqual(['yes', 'yes2'])
  })

  it('is empty for an empty queue', () => {
    expect(highConfidenceAcceptable([])).toEqual([])
  })
})
