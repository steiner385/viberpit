import { describe, it, expect } from 'vitest'
import { bandSegments, pseudoElapsedFromPercent, liveElapsedS, isOverrun, expectedTotals, ganttScale, ganttSegments } from '@/lib/etaBand'

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

describe('expectedTotals', () => {
  it('adds remaining estimates onto the elapsed snapshot', () => {
    expect(expectedTotals({ duration_seconds: 60, rem_low_seconds: 10, rem_nominal_seconds: 30, rem_high_seconds: 90 }))
      .toEqual({ expLow: 70, expNominal: 90, expHigh: 150 })
  })
  it('returns null for absent remaining fields and treats null duration as 0', () => {
    expect(expectedTotals({ duration_seconds: null })).toEqual({ expLow: null, expNominal: null, expHigh: null })
    expect(expectedTotals({ duration_seconds: null, rem_high_seconds: 45 }).expHigh).toBe(45)
  })
})

describe('ganttScale', () => {
  it('is the max of elapsed and expHigh across all rows', () => {
    const rows = [
      { elapsed: 26, expLow: null, expNominal: null, expHigh: null },
      { elapsed: 40, expLow: 60, expNominal: 120, expHigh: 180 },
    ]
    expect(ganttScale(rows)).toBe(180)
  })
  it('floors at 60 when everything is zero', () => {
    expect(ganttScale([{ elapsed: 0, expLow: null, expNominal: null, expHigh: null }])).toBe(60)
  })
})

describe('ganttSegments', () => {
  it('normalizes fill against the shared scale so a 26s bar is ~0.14x a 180s scale', () => {
    const seg = ganttSegments({ elapsed: 26, expLow: null, expNominal: null, expHigh: null }, 180)
    expect(seg.fillPct).toBeCloseTo((26 / 180) * 100, 4)
    expect(seg.bandLeftPct).toBeNull()
    expect(seg.tickPct).toBeNull()
  })
  it('computes band + tick positions on the shared scale', () => {
    const seg = ganttSegments({ elapsed: 40, expLow: 60, expNominal: 120, expHigh: 180 }, 180)
    expect(seg.bandLeftPct).toBeCloseTo((60 / 180) * 100, 4)
    expect(seg.bandWidthPct).toBeCloseTo(((180 - 60) / 180) * 100, 4)
    expect(seg.tickPct).toBeCloseTo((120 / 180) * 100, 4)
  })
  it('forces a 15% stub for queued rows regardless of elapsed', () => {
    expect(ganttSegments({ elapsed: 0, expLow: null, expNominal: null, expHigh: null }, 180, true).fillPct).toBe(15)
  })
  it('clamps fill and band to 100%', () => {
    const seg = ganttSegments({ elapsed: 500, expLow: 400, expNominal: 450, expHigh: 500 }, 180)
    expect(seg.fillPct).toBe(100)
    expect(seg.bandLeftPct).toBe(100)
  })
})
