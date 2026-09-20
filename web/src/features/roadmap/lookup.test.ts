// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { MapChangesPointer } from '@/api/roadmap'
import { MAP, product } from './__fixtures__/map'
import type { RoadmapMap } from '@/api/roadmap'
import { findProduct, findCapability, capabilityTrail, flattenCapabilities, openMapChanges, programTitleOf } from './lookup'

describe('lookup', () => {
  it('finds products and nested capabilities', () => {
    const p = findProduct(MAP, 'kindash')!
    expect(p.name).toBe('KinDash')
    expect(findProduct(MAP, 'nope')).toBeNull()
    expect(findCapability(p, 'task-views')?.title).toBe('Task views')
    expect(findCapability(p, 'nope')).toBeNull()
    expect(capabilityTrail(p, 'task-views').map((c) => c.slug)).toEqual(['tasks', 'task-views'])
    expect(capabilityTrail(p, 'nope')).toEqual([])
  })

  // A malformed-but-truthy `/pm/map` payload (missing/non-array `products`)
  // must degrade to "not found", not throw and take the page down with it.
  it('degrades to empty results on a malformed map instead of throwing', () => {
    const malformed = { generatedAt: 'x' } as unknown as RoadmapMap
    expect(findProduct(malformed, 'kindash')).toBeNull()
    expect(() => programTitleOf(malformed)('kindash')).not.toThrow()
  })

  // The one flattening both capability pickers use — "+ Initiative" and the
  // Inbox's proposal editor — so the indent cannot drift between them.
  it('flattens the capability tree depth-first, parents before children', () => {
    const p = findProduct(MAP, 'kindash')!
    expect(flattenCapabilities(p.capabilities).map((c) => [c.node.slug, c.depth]))
      .toEqual([['billing', 0], ['tasks', 0], ['task-views', 1]])
    expect(flattenCapabilities([])).toEqual([])
  })
})

/**
 * The one reading of "is there a live map-changes PR?", shared by the product
 * Overview's card and a capability's "Changes pending" note — two surfaces that
 * used to check the status inline and could have drifted into showing a dead PR.
 */
describe('openMapChanges', () => {
  const pointer = (over: Partial<MapChangesPointer> = {}): MapChangesPointer => ({
    branch: 'pm/map-changes-kindash', prNumber: 12, prUrl: 'https://example.test/pull/12',
    status: 'open', openedAt: '2026-09-11T00:00:00Z', headSha: 'abc123',
    behind: false, conflicting: false, ...over,
  })
  const withPointer = (p: MapChangesPointer | null) => product({ slug: 'kindash', mapChanges: p })

  it('returns the pointer only while it is open', () => {
    const open = pointer()
    expect(openMapChanges(withPointer(open))).toBe(open)
    // Merged and abandoned are history the map already reflects; showing either
    // would offer Rebase and Abandon on a PR that is no longer there.
    expect(openMapChanges(withPointer(pointer({ status: 'merged' })))).toBeNull()
    expect(openMapChanges(withPointer(pointer({ status: 'abandoned' })))).toBeNull()
    // The server's status is a plain string on the wire — an unknown value is
    // not "open".
    expect(openMapChanges(withPointer(pointer({ status: 'something-new' })))).toBeNull()
  })

  it('is null without a product or without a pointer', () => {
    expect(openMapChanges(withPointer(null))).toBeNull()
    expect(openMapChanges(null)).toBeNull()
    expect(openMapChanges(undefined)).toBeNull()
  })
})
