// "Needs you" is every pending human decision AND every stall a human can clear
// (design spec item 17): needs-you rows first, blocked rows after them.
import { describe, it, expect } from 'vitest'
import { cap, ini, product } from '../__fixtures__/map'
import { needsYouCount, needsYouRows } from './needsYou'

const p = product({
  slug: 'kindash', name: 'KinDash',
  initiatives: [
    ini({ slug: 'stuck', projectSlug: 'kindash', tier: 'blocked', reason: 'epic_blocked' }),
    ini({ slug: 'ready', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
    ini({ slug: 'moving', projectSlug: 'kindash', tier: 'in-flight' }),
  ],
})

describe('needsYouRows', () => {
  it('lists needs-you rows first, then blocked rows, never in-flight ones', () => {
    const rows = needsYouRows([p]).initiatives.map((r) => r.initiative.slug)
    expect(rows).toEqual(['ready', 'stuck'])
  })
  it('counts what it lists', () => {
    expect(needsYouCount([p])).toBe(2)
  })
})

const child = cap({ slug: 'child', counts: { openIssues: 2, inFlight: 0, blocked: 1, needsYou: 1 } })
const parent = cap({ slug: 'parent', children: [child], counts: { openIssues: 3, inFlight: 0, blocked: 2, needsYou: 3 } })
const scopedProduct = product({ slug: 'p', capabilities: [parent], initiatives: [
  ini({ slug: 'stalled', projectSlug: 'p', primaryCapability: 'parent', tier: 'blocked' }),
  ini({ slug: 'review', projectSlug: 'p', primaryCapability: 'parent', tier: 'needs-you' }),
] })

it('counts direct issue groups without counting child rollups or primary initiatives twice', () => {
  const rows = needsYouRows([scopedProduct])
  expect(rows.issues.map(({ capability, needsYou, blocked }) => [capability.slug, needsYou, blocked]))
    .toEqual([['parent', 1, 0], ['child', 1, 1]])
  expect(needsYouCount([scopedProduct])).toBe(4)
})

it('keeps blocked initiatives and descendant issue groups in capability scope', () => {
  expect(needsYouRows([scopedProduct], parent).initiatives.map(({ initiative }) => initiative.slug))
    .toEqual(['review', 'stalled'])
  expect(needsYouCount([scopedProduct], child)).toBe(1)
})
