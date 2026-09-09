// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { MAP, ini, product } from '../__fixtures__/map'
import { inFlightCount, inFlightInitiatives } from './inFlight'

describe('inFlight', () => {
  it('counts every initiative that is moving or stuck, across products', () => {
    // kindash: `views` (in-flight) + `orphan` (needs-you); viberpit: none.
    expect(inFlightCount(MAP.products)).toBe(2)
    expect(inFlightInitiatives(MAP.products).map((i) => i.slug)).toEqual(['views', 'orphan'])
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
