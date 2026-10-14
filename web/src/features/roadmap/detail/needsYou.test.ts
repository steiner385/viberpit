// "Needs you" is only what a human alone can unblock (design 2026-10-14 §1):
// the needs-you tier. Blocked rows are WAITING — listed by `waitingRows`, never
// counted.
import { describe, it, expect } from 'vitest'
import { cap, ini, product } from '../__fixtures__/map'
import { needsYouCount, needsYouRows, waitingRows, waitingSummary } from './needsYou'

const p = product({
  slug: 'kindash', name: 'KinDash',
  initiatives: [
    ini({ slug: 'stuck', projectSlug: 'kindash', tier: 'blocked', reason: 'epic_blocked' }),
    ini({ slug: 'ready', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
    ini({ slug: 'moving', projectSlug: 'kindash', tier: 'in-flight' }),
  ],
})

describe('needsYouRows', () => {
  it('lists only needs-you rows — blocked ones wait, in-flight ones move', () => {
    expect(needsYouRows([p]).initiatives.map((r) => r.initiative.slug)).toEqual(['ready'])
    expect(waitingRows([p]).initiatives.map((r) => r.initiative.slug)).toEqual(['stuck'])
  })
  it('counts what it lists, and never a waiting row', () => {
    expect(needsYouCount([p])).toBe(1)
  })
})

describe('programs count for their OWN tier, never once more per phase', () => {
  const prog = product({ slug: 'p', initiatives: [
    // Raised to needs-you by its phase; its own tier is in-flight.
    ini({ slug: 'prog', projectSlug: 'p', hasChildren: true, tier: 'needs-you', ownTier: 'in-flight' }),
    ini({ slug: 'phase', projectSlug: 'p', parentInitiative: 'prog', tier: 'needs-you', ownTier: 'needs-you' }),
    // Its own gate needs you.
    ini({ slug: 'gate', projectSlug: 'p', hasChildren: true, tier: 'needs-you', ownTier: 'needs-you' }),
  ] })
  it('lists the phase and the gated program, not the raised one', () => {
    expect(needsYouRows([prog]).initiatives.map((r) => r.initiative.slug)).toEqual(['gate', 'phase'])
    expect(needsYouCount([prog])).toBe(2)
  })
  it('falls back to tier when an older server sends no ownTier', () => {
    const old = product({ slug: 'o', initiatives: [ini({ slug: 'x', projectSlug: 'o', tier: 'needs-you' })] })
    expect(needsYouCount([old])).toBe(1)
  })
})

describe('waitingSummary', () => {
  it('lists non-zero waiting counters in WAITING_KEYS order with their words', () => {
    const w = product({ slug: 'w', counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0,
      waiting: { blocked: 31, boardBlocked: 0, boardHeld: 0, parked: 28, epicOpen: 8, inProgress: 0, tracker: 0,
        notNeeded: 0, assigned: 0, offQueue: 0, done: 0, bot: 0, untrusted: 0, environment: 0 } } })
    expect(waitingSummary(w)).toEqual([
      { key: 'blocked', label: 'blocked by a dependency', count: 31 },
      { key: 'parked', label: 'parked', count: 28 },
      { key: 'epicOpen', label: 'epic with open sub-issues', count: 8 },
    ])
  })
  it('is empty for an older server that sends no waiting counters', () => {
    expect(waitingSummary(product({ slug: 'old' }))).toEqual([])
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

  it('sorts waiting rows newest first too, apart from the needs-you ones', () => {
    const prod = product({ slug: 'kindash', initiatives: [
      dated('old-block', '2026-09-01T00:00:00Z', 'blocked'),
      dated('fresh-block', '2026-09-19T00:00:00Z', 'blocked'),
      dated('stale-decision', '2026-09-01T00:00:00Z', 'needs-you'),
    ] })
    expect(needsYouRows([prod]).initiatives.map((r) => r.initiative.slug)).toEqual(['stale-decision'])
    expect(waitingRows([prod]).initiatives.map((r) => r.initiative.slug)).toEqual(['fresh-block', 'old-block'])
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
  expect(needsYouRows([scopedProduct]).issues.map(({ capability, needsYou }) => [capability.slug, needsYou]))
    .toEqual([['parent', 1], ['child', 1]])
  expect(waitingRows([scopedProduct]).issues.map(({ capability, blocked }) => [capability.slug, blocked]))
    .toEqual([['child', 1]])
  // review + parent's 1 + child's 1; the blocked issue and initiative wait.
  expect(needsYouCount([scopedProduct])).toBe(3)
})

it('keeps initiatives and descendant issue groups in capability scope', () => {
  expect(needsYouRows([scopedProduct], parent).initiatives.map(({ initiative }) => initiative.slug))
    .toEqual(['review'])
  expect(waitingRows([scopedProduct], parent).initiatives.map(({ initiative }) => initiative.slug))
    .toEqual(['stalled'])
  expect(needsYouCount([scopedProduct], child)).toBe(1)
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
    expect(needsYouRows([scoped]).issues.map(({ capability, needsYou }) => [capability.slug, needsYou]))
      .toEqual([['root', 1], ['child', 1]])
    expect(waitingRows([scoped]).issues.map(({ capability, blocked }) => [capability.slug, blocked]))
      .toEqual([['root', 1], ['child', 1]])
    // primary + touching + program (no ownTier: falls back to tier) + 2 issues.
    expect(needsYouCount([scoped])).toBe(5)
  })

  it('does not turn in-flight issues or inconsistent negative rollups into attention', () => {
    const child = cap({ slug: 'child', counts: { openIssues: 1, inFlight: 0, needsYou: 0, blocked: 1 } })
    const scoped = product({ slug: 'p', capabilities: [
      cap({ slug: 'root', children: [child] }),
      cap({ slug: 'moving', counts: { openIssues: 2, inFlight: 2, needsYou: 0, blocked: 0 } }),
    ] })
    expect(needsYouRows([scoped]).issues).toEqual([])
    expect(waitingRows([scoped]).issues.map(({ capability, blocked }) => [capability.slug, blocked]))
      .toEqual([['child', 1]])
    expect(needsYouCount([scoped])).toBe(0)
    expect(needsYouRows([product({ slug: 'empty' })]).issues).toEqual([])
  })
})
