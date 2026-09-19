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

describe('needsYouRows — newest first inside a tier (#1476, spec §7)', () => {
  // The map arrives created_at ASCENDING, which is the opposite of what §7 asks
  // for, and `updatedAt` is the only timestamp a node carries.
  const dated = (slug: string, updatedAt: string | null, tier: 'needs-you' | 'blocked' = 'needs-you') =>
    ini({ slug, projectSlug: 'kindash', tier, updatedAt })

  it('sorts a tier newest first', () => {
    const prod = product({ slug: 'kindash', initiatives: [
      dated('oldest', '2026-09-01T00:00:00Z'),
      dated('newest', '2026-09-19T00:00:00Z'),
      dated('middle', '2026-09-10T00:00:00Z'),
    ] })
    expect(needsYouRows([prod]).initiatives.map((r) => r.initiative.slug))
      .toEqual(['newest', 'middle', 'oldest'])
  })

  it('keeps the TIER as the primary order — a stale decision still outranks a fresh block', () => {
    const prod = product({ slug: 'kindash', initiatives: [
      dated('fresh-block', '2026-09-19T00:00:00Z', 'blocked'),
      dated('stale-decision', '2026-09-01T00:00:00Z', 'needs-you'),
    ] })
    expect(needsYouRows([prod]).initiatives.map((r) => r.initiative.slug))
      .toEqual(['stale-decision', 'fresh-block'])
  })

  it('sorts ACROSS products within a tier, not per product', () => {
    // One queue, not "newest in KinDash, then newest in ViberPit".
    const a = product({ slug: 'a', initiatives: [dated('a-old', '2026-09-01T00:00:00Z')] })
    const b = product({ slug: 'b', initiatives: [dated('b-new', '2026-09-19T00:00:00Z')] })
    expect(needsYouRows([a, b]).initiatives.map((r) => r.initiative.slug))
      .toEqual(['b-new', 'a-old'])
  })

  it('sorts a node with no timestamp LAST, and breaks ties by slug', () => {
    // An absent `updatedAt` (an older server, a hand-built fixture) must never
    // jump the queue; equal timestamps must not reshuffle between renders.
    const prod = product({ slug: 'kindash', initiatives: [
      dated('undated', null),
      dated('zebra', '2026-09-10T00:00:00Z'),
      dated('apple', '2026-09-10T00:00:00Z'),
    ] })
    expect(needsYouRows([prod]).initiatives.map((r) => r.initiative.slug))
      .toEqual(['apple', 'zebra', 'undated'])
  })
})

const child = cap({ slug: 'child', counts: { openIssues: 2, inFlight: 0, blocked: 1, needsYou: 1 } })
const parent = cap({ slug: 'parent', children: [child], counts: { openIssues: 3, inFlight: 0, blocked: 2, needsYou: 3 } })
const scopedProduct = product({ slug: 'p', capabilities: [parent], initiatives: [
  ini({ slug: 'stalled', projectSlug: 'p', primaryCapability: 'parent', tier: 'blocked' }),
  ini({ slug: 'review', projectSlug: 'p', primaryCapability: 'parent', tier: 'needs-you' }),
] })

it('counts individual direct issues without counting child rollups or primary initiatives twice', () => {
  const rows = needsYouRows([scopedProduct])
  expect(rows.issues.map(({ capability, needsYou, blocked }) => [capability.slug, needsYou, blocked]))
    .toEqual([['parent', 1, 0], ['child', 1, 1]])
  expect(needsYouCount([scopedProduct])).toBe(5)
})

it('keeps blocked initiatives and descendant issue groups in capability scope', () => {
  expect(needsYouRows([scopedProduct], parent).initiatives.map(({ initiative }) => initiative.slug))
    .toEqual(['review', 'stalled'])
  expect(needsYouCount([scopedProduct], child)).toBe(2)
})

describe('issue attention counts', () => {
  it('counts nested direct issues once, excluding primary leaf initiatives but not touching initiatives or programs', () => {
    const child = cap({ slug: 'child', counts: { openIssues: 2, inFlight: 0, needsYou: 1, blocked: 2 } })
    const root = cap({ slug: 'root', children: [child], counts: { openIssues: 4, inFlight: 0, needsYou: 3, blocked: 3 } })
    const scoped = product({ slug: 'p', capabilities: [root], initiatives: [
      ini({ slug: 'primary', projectSlug: 'p', primaryCapability: 'root', tier: 'needs-you' }),
      ini({ slug: 'blocked-primary', projectSlug: 'p', primaryCapability: 'child', tier: 'blocked' }),
      ini({ slug: 'touching', projectSlug: 'p', primaryCapability: 'elsewhere', capabilities: ['root'], tier: 'needs-you' }),
      ini({ slug: 'program', projectSlug: 'p', primaryCapability: 'root', hasChildren: true, tier: 'needs-you' }),
    ] })
    expect(needsYouRows([scoped]).issues.map(({ capability, needsYou, blocked }) => [capability.slug, needsYou + blocked]))
      .toEqual([['root', 2], ['child', 2]])
    expect(needsYouCount([scoped])).toBe(8)
  })

  it('does not turn in-flight issues or inconsistent negative rollups into attention', () => {
    const child = cap({ slug: 'child', counts: { openIssues: 1, inFlight: 0, needsYou: 0, blocked: 1 } })
    const scoped = product({ slug: 'p', capabilities: [
      cap({ slug: 'root', children: [child] }),
      cap({ slug: 'moving', counts: { openIssues: 2, inFlight: 2, needsYou: 0, blocked: 0 } }),
    ] })
    expect(needsYouRows([scoped]).issues.map(({ capability, needsYou, blocked }) => [capability.slug, needsYou + blocked]))
      .toEqual([['child', 1]])
    expect(needsYouCount([scoped])).toBe(1)
    expect(needsYouRows([product({ slug: 'empty' })]).issues).toEqual([])
  })
})
