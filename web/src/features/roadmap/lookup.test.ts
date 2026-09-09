// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP } from './__fixtures__/map'
import { findProduct, findCapability, capabilityTrail, flattenCapabilities } from './lookup'

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

  // The one flattening both capability pickers use — "+ Initiative" and the
  // Inbox's proposal editor — so the indent cannot drift between them.
  it('flattens the capability tree depth-first, parents before children', () => {
    const p = findProduct(MAP, 'kindash')!
    expect(flattenCapabilities(p.capabilities).map((c) => [c.node.slug, c.depth]))
      .toEqual([['billing', 0], ['tasks', 0], ['task-views', 1]])
    expect(flattenCapabilities([])).toEqual([])
  })
})
