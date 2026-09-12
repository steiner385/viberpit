// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP, ini, product } from '../__fixtures__/map'
import { inFlightCount, inFlightInitiatives } from './inFlight'

describe('inFlight', () => {
  it('counts every LEAF initiative that is moving or stuck, across products', () => {
    // kindash: `views` (in-flight) + `orphan` (needs-you) + the `audit`
    // program's `audit-01` phase (in-flight); viberpit: none. The `audit`
    // program itself is EXCLUDED — leaf-only, so a program and its phase are
    // never counted as two separate in-flight items.
    expect(inFlightCount(MAP.products)).toBe(3)
    expect(inFlightInitiatives(MAP.products).map((i) => i.slug)).toEqual(['views', 'orphan', 'audit-01'])
  })

  it('excludes a program even when it is itself in an in-flight tier', () => {
    const p = product({
      slug: 'q',
      initiatives: [
        ini({ slug: 'program', projectSlug: 'q', tier: 'in-flight', hasChildren: true }),
        ini({ slug: 'phase', projectSlug: 'q', tier: 'in-flight', parentInitiative: 'program' }),
      ],
    })
    expect(inFlightInitiatives([p]).map((i) => i.slug)).toEqual(['phase'])
  })

  it('counts blocked and needs-you as in flight, and history as not', () => {
    const p = product({
      slug: 'p',
      initiatives: [
        ini({ slug: 'moving', projectSlug: 'p', tier: 'in-flight' }),
        ini({ slug: 'stuck', projectSlug: 'p', tier: 'blocked' }),
        ini({ slug: 'waiting', projectSlug: 'p', tier: 'needs-you' }),
        ini({ slug: 'shipped', projectSlug: 'p', tier: 'live' }),
        ini({ slug: 'idea', projectSlug: 'p', tier: 'proposed' }),
        ini({ slug: 'cold', projectSlug: 'p', tier: 'idle' }),
      ],
    })
    expect(inFlightInitiatives([p]).map((i) => i.slug)).toEqual(['moving', 'stuck', 'waiting'])
  })

  it('is zero for no products at all', () => {
    expect(inFlightCount([])).toBe(0)
  })
})
