import { describe, it, expect } from 'vitest'
import { bandSegments, pseudoElapsedFromPercent } from '@/lib/etaBand'

describe('bandSegments', () => {
  it('computes solid/band/tick from elapsed + remaining bounds', () => {
    // elapsed 4m, low 1m, nominal 3m, high 7m → scale = 4+7 = 11m
    const s = bandSegments({ elapsedS: 240, lowS: 60, nominalS: 180, highS: 420 })!
    expect(Math.round(s.solidPct)).toBe(Math.round((240 / 660) * 100))    // 36
    expect(Math.round(s.tickPct)).toBe(Math.round((420 / 660) * 100))     // nominal completion 7m
    expect(Math.round(s.bandLeftPct)).toBe(Math.round((300 / 660) * 100)) // low completion 5m
    expect(s.bandWidthPct).toBeCloseTo(100 - s.bandLeftPct, 5)            // band ends at scale
  })
  it('returns null when a bound is missing', () => {
    // @ts-expect-error intentional null
    expect(bandSegments({ elapsedS: 100, lowS: null, nominalS: 50, highS: 80 })).toBeNull()
  })
  it('pseudoElapsedFromPercent grows with percent', () => {
    expect(pseudoElapsedFromPercent(300, 50)).toBeCloseTo(300, 5)        // 300*50/50
    expect(pseudoElapsedFromPercent(300, 0)).toBe(0)
  })
})
