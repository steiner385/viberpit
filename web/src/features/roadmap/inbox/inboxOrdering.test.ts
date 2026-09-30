/**
 * The Inbox's ONE ordering spec, asked directly about the two card kinds it now
 * carries (spec 2026-09-18 §6: decision proposals list with classification
 * proposals, "same ordering spec and keys").
 *
 * `InboxScopeBody.test.tsx` covers the rendered list; this file covers the
 * branches that only the groupings a test would have to click a control to
 * reach can show — capability and confidence, neither of which a decision has.
 */
import { describe, it, expect } from 'vitest'
import type { DecisionProposal, InboxProposal, TriageProposal } from '@/api/triage'
import { INBOX_ORDERING_SPEC } from './inboxOrdering'

const triage = (over: Partial<TriageProposal> = {}): TriageProposal => ({
  proposalKind: 'triage', id: 't1', workItemId: 'w1', projectSlug: 'kindash',
  repo: 'cairnea/KinDash', number: 1, kind: 'issue', title: 'Bulk edit', url: null,
  proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: null, impact: null, effort: null, confidence: null, rationale: null,
  confidenceLevel: 'high', status: 'pending', createdAt: null, classifiedBy: null,
  resolvedAt: null, ...over,
})

const decision = (over: Partial<DecisionProposal> = {}): DecisionProposal => ({
  proposalKind: 'decision', id: 'd1', workItemId: 'w9', repo: 'cairnea/KinDash',
  number: 12, ref: 'cairnea/KinDash#12', projectSlug: 'kindash',
  title: 'Queue or stream?', url: null,
  options: [{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }],
  recommendedKey: 'a', rationale: null, chosenKey: null, status: 'pending',
  proposedBy: 'session:s1', resolvedBy: null, createdAt: null, resolvedAt: null,
  ...over,
})

const bucket = (grouping: 'product' | 'capability' | 'confidence', p: InboxProposal) =>
  INBOX_ORDERING_SPEC.config.groupings[grouping]!.bucketOf(p)
const sortBy = (key: 'newest' | 'confidence' | 'title', rows: InboxProposal[]) =>
  [...rows].sort(INBOX_ORDERING_SPEC.config.sorts[key]!).map((p) => p.id)

describe('INBOX_ORDERING_SPEC over both card kinds', () => {
  it('groups both by the product that owns the item — the fact they share', () => {
    expect(bucket('product', triage())).toMatchObject({ key: 'kindash' })
    expect(bucket('product', decision())).toMatchObject({ key: 'kindash' })
  })

  it('gives a decision its own capability bucket, last', () => {
    // Not "no fit": a decision was never offered a capability, and filing it
    // under the classifier's leftovers would say something untrue about it.
    expect(bucket('capability', triage())).toMatchObject({ key: 'tasks', order: 0 })
    expect(bucket('capability', triage({ proposedFeatureSlug: null })))
      .toMatchObject({ key: 'no-fit', order: 1 })
    expect(bucket('capability', decision()))
      .toEqual({ key: 'kind:decision', label: 'Decisions', order: 2 })
  })

  it('namespaces the decision bucket so a capability slug cannot collide with it', () => {
    // The other capability buckets are keyed on user data. A capability actually
    // slugged `decision` shared the bare key, arriving with a different label and
    // a different order — and the collapsed-state store persists bucket keys, so
    // the two would have been confused across sessions.
    const collide = bucket('capability', triage({ proposedFeatureSlug: 'decision' }))
    expect(collide).toMatchObject({ key: 'decision', label: 'decision', order: 0 })
    expect(bucket('capability', decision()).key).not.toBe(collide.key)
  })

  it('gives a decision its own confidence bucket, after every level', () => {
    expect(bucket('confidence', triage({ confidenceLevel: 'low' }))).toMatchObject({ order: 2 })
    expect(bucket('confidence', decision()))
      .toEqual({ key: 'kind:decision', label: 'Decisions', order: 3 })
  })

  it('sorts a decision after every scored row, and by title like any other', () => {
    const rows = [decision({ id: 'd' }), triage({ id: 't', confidenceLevel: 'low' })]
    expect(sortBy('confidence', rows)).toEqual(['t', 'd'])
    expect(sortBy('title', [
      decision({ id: 'd', title: 'Apples' }), triage({ id: 't', title: 'Bananas' }),
    ])).toEqual(['d', 't'])
  })

  it('sorts newest by createdAt across both kinds', () => {
    expect(sortBy('newest', [
      triage({ id: 'older', createdAt: '2026-09-01T00:00:00Z' }),
      decision({ id: 'newer', createdAt: '2026-09-18T00:00:00Z' }),
    ])).toEqual(['newer', 'older'])
  })

  it('a decision that blocks more work leads its bucket under every sort; triage rows keep their order', () => {
    const rows = [
      triage({ id: 't1', createdAt: '2026-01-03' }),
      decision({ id: 'd-low', downstream: 1, createdAt: '2026-01-02' }),
      decision({ id: 'd-high', downstream: 7, createdAt: '2026-01-01' }),
      decision({ id: 'd-none', createdAt: '2026-01-04' }),
    ]
    for (const key of ['newest', 'confidence', 'title'] as const) {
      expect(sortBy(key, rows).slice(0, 2)).toEqual(['d-high', 'd-low'])
    }
    // An undefined downstream counts as 0; ties fall through to newest.
    expect(sortBy('newest', rows)).toEqual(['d-high', 'd-low', 'd-none', 't1'])
  })
})
