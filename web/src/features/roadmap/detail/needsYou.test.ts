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
    expect(needsYouRows([scoped]).issueGroups.map(({ capability, count }) => [capability.slug, count]))
      .toEqual([['root', 2], ['child', 2]])
    expect(needsYouCount([scoped])).toBe(8)
  })

  it('does not turn in-flight issues or inconsistent negative rollups into attention', () => {
    const child = cap({ slug: 'child', counts: { openIssues: 1, inFlight: 0, needsYou: 0, blocked: 1 } })
    const scoped = product({ slug: 'p', capabilities: [
      cap({ slug: 'root', children: [child] }),
      cap({ slug: 'moving', counts: { openIssues: 2, inFlight: 2, needsYou: 0, blocked: 0 } }),
    ] })
    expect(needsYouRows([scoped]).issueGroups.map(({ capability, count }) => [capability.slug, count]))
      .toEqual([['child', 1]])
    expect(needsYouCount([scoped])).toBe(1)
    expect(needsYouRows([product({ slug: 'empty' })]).issueGroups).toEqual([])
  })
})
