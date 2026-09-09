// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP } from './__fixtures__/map'
import { findProduct, findCapability, capabilityTrail } from './lookup'

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
})
