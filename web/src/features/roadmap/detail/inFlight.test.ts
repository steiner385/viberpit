// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP, ini, product } from '../__fixtures__/map'
import { inFlightCount, inFlightInitiatives } from './inFlight'

describe('inFlight', () => {
  it('counts every initiative that is moving or stuck, across products', () => {
    // kindash: `views` (in-flight) + `orphan` (needs-you) + the `audit` program
    // and its `audit-01` phase (both default to in-flight); viberpit: none.
    // This count is tier-only — it does not know about program/phase nesting,
    // so a program and its phase are each counted separately, same as any
    // other two initiatives.
    expect(inFlightCount(MAP.products)).toBe(4)
    expect(inFlightInitiatives(MAP.products).map((i) => i.slug)).toEqual(['views', 'orphan', 'audit', 'audit-01'])
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
