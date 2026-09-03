import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier } from './statusTier'

describe('retrying presentation', () => {
  it('is amber, busy tier, overloaded label', () => {
    const p = statusPresentation('retrying')
    expect(p.label).toBe('Overloaded — retrying')
    expect(p.tier).toBe('busy')
    expect(p.textClass).toContain('warning')
  })
  it('unknown status still falls back to Ready', () => {
    expect(statusTier('nonsense')).toBe('your-turn')
  })
})

describe('interrupted-by-restart presentation (issue #434)', () => {
  it('relabels an idle session as an actionable re-send prompt', () => {
    const p = statusPresentation('idle', { interruptedByRestart: true })
    expect(p.tier).toBe('needs-you')
    expect(p.label).toMatch(/re-send/i)
  })
  it('also relabels a needs_attention session interrupted by a restart', () => {
    const p = statusPresentation('needs_attention', { interruptedByRestart: true })
    expect(p.tier).toBe('needs-you')
    expect(p.label).toMatch(/re-send/i)
  })
  it('leaves a plain idle session as the calm Ready pill', () => {
    expect(statusPresentation('idle').label).toBe('Ready')
    expect(statusPresentation('idle', { interruptedByRestart: false }).label).toBe('Ready')
  })
  it('does not hijack an in_progress session even if the flag lingers', () => {
    expect(statusPresentation('in_progress', { interruptedByRestart: true }).label).toBe('Working…')
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
