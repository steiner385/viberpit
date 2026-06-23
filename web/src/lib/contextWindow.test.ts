import { describe, it, expect } from 'vitest'
import { modelContextWindow, remainingContextPct, contextSeverity } from './contextWindow'

describe('modelContextWindow', () => {
  it('defaults to 200k for a normal model id', () => {
    expect(modelContextWindow('claude-sonnet-4-5')).toBe(200_000)
  })

  it('returns 1M for a model id carrying the [1m] long-context marker', () => {
    expect(modelContextWindow('claude-sonnet-4-5[1m]')).toBe(1_000_000)
  })

  it('defaults to 200k when the model is null/unknown', () => {
    expect(modelContextWindow(null)).toBe(200_000)
  })
})

describe('remainingContextPct', () => {
  it('returns null when usage is null', () => {
    expect(remainingContextPct(null, 'claude-sonnet-4-5')).toBeNull()
  })

  it('computes remaining % from input + cacheRead against the window', () => {
    // 50k of a 200k window used → 75% remaining
    const usage = { input: 10_000, output: 999, cacheRead: 40_000, costUsd: 0.1 }
    expect(remainingContextPct(usage, 'claude-sonnet-4-5')).toBe(75)
  })

  it('uses the 1M window for [1m] models', () => {
    // 250k used of 1M → 75% remaining
    const usage = { input: 50_000, output: 0, cacheRead: 200_000, costUsd: 0 }
    expect(remainingContextPct(usage, 'claude-opus-4-1[1m]')).toBe(75)
  })

  it('clamps to 0 when usage exceeds the window (never negative)', () => {
    const usage = { input: 300_000, output: 0, cacheRead: 0, costUsd: 0 }
    expect(remainingContextPct(usage, 'claude-sonnet-4-5')).toBe(0)
  })
})

describe('contextSeverity', () => {
  it('is neutral when plenty remains or unknown', () => {
    expect(contextSeverity(null)).toBe('neutral')
    expect(contextSeverity(80)).toBe('neutral')
    expect(contextSeverity(26)).toBe('neutral')
  })
  it('warns in the mid band', () => {
    expect(contextSeverity(25)).toBe('warning')
    expect(contextSeverity(11)).toBe('warning')
  })
  it('is danger when nearly full', () => {
    expect(contextSeverity(10)).toBe('danger')
    expect(contextSeverity(2)).toBe('danger')
  })
})
