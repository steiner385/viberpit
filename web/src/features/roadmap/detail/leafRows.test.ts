import { describe, it, expect } from 'vitest'
import type { Leaf } from '@/api/roadmap'
import { leafRows } from './leafRows'

const leaf = (id: string, state: string): Leaf => ({
  id, ref: `o/r#${id}`, repo: 'o/r', number: Number(id), kind: 'issue', title: null, url: null, state,
  triageState: null, score: null, reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
  blockedBy: [], parentRef: null, sessionStatus: null, ghCreatedAt: null, featureId: null,
  reason: null, bounces: 0,
})

describe('leafRows', () => {
  it('puts open leaves under an Open header and closed under a Closed one, each counted', () => {
    const rows = leafRows([leaf('1', 'open'), leaf('2', 'closed'), leaf('3', 'open')])
    expect(rows.map((r) => (r.kind === 'header' ? r.label : r.leaf.id))).toEqual(['Open · 2', '1', '3', 'Closed · 1', '2'])
    expect(rows.map((r) => r.key)).toEqual(['h:open', '1', '3', 'h:closed', '2'])
  })
  it('omits an empty section', () => {
    expect(leafRows([leaf('1', 'open')]).map((r) => r.key)).toEqual(['h:open', '1'])
    expect(leafRows([leaf('1', 'closed')]).map((r) => r.key)).toEqual(['h:closed', '1'])
    expect(leafRows([])).toEqual([])
  })
})

it('brings actionable open issues into view before ordinary work, preserving order within each group', () => {
  const rows = leafRows([
    leaf('1', 'open'),
    { ...leaf('2', 'open'), tier: 'blocked' },
    { ...leaf('3', 'open'), tier: 'needs-you' },
    { ...leaf('4', 'open'), tier: 'needs-you' },
    { ...leaf('5', 'closed'), tier: 'needs-you' },
    { ...leaf('6', 'open'), tier: 'in-flight' },
  ])
  expect(rows.map((r) => r.key)).toEqual(['h:open', '3', '4', '2', '1', '6', 'h:closed', '5'])
})
