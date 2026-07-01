import { describe, it, expect } from 'vitest'
import { bandSegments, pseudoElapsedFromPercent, liveElapsedS, isOverrun } from '@/lib/etaBand'

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

describe('liveElapsedS', () => {
  it('returns the snapshot when now == updatedAt (delta 0)', () => {
    expect(liveElapsedS(120, 1_000_000, 1_000_000)).toBe(120)
  })
  it('advances by whole seconds since the poll receive time', () => {
    expect(liveElapsedS(120, 1_000_000, 1_030_000)).toBeCloseTo(150, 5) // +30s
  })
  it('floors the delta at 0 when now < updatedAt', () => {
    expect(liveElapsedS(120, 1_000_000, 999_000)).toBe(120)
  })
})

describe('isOverrun', () => {
  it('is false when remHigh is null', () => {
    expect(isOverrun(999, null)).toBe(false)
  })
  it('is false while the delta is below remHigh', () => {
    expect(isOverrun(40, 60)).toBe(false)
  })
  it('is true once the delta reaches remHigh', () => {
    expect(isOverrun(60, 60)).toBe(true)
    expect(isOverrun(75, 60)).toBe(true)
  })
})
