import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier } from './statusTier'

describe('retrying presentation', () => {
  it('is amber, busy tier, overloaded label', () => {
    const p = statusPresentation('retrying')
    expect(p.label).toBe('Overloaded — retrying')
    expect(p.tier).toBe('busy')
    expect(p.textClass).toContain('amber')
  })
  it('unknown status still falls back to Ready', () => {
    expect(statusTier('nonsense')).toBe('your-turn')
  })
})

describe('condensing status', () => {
  it('presents as a busy-tier Condensing pill', () => {
    const p = statusPresentation('condensing')
    expect(p.tier).toBe('busy')
    expect(p.label).toBe('Condensing…')
  })
  it('is grouped with the busy tier', () => {
    expect(statusTier('condensing')).toBe('busy')
  })
})
