import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier, TIER_ACCENT } from './statusTier'
import type { StatusTier } from './statusTier'

describe('retrying presentation', () => {
  it('is amber, busy tier, overloaded label', () => {
    const p = statusPresentation('retrying')
    expect(p.label).toBe('Overloaded — retrying')
    expect(p.tier).toBe('busy')
    expect(p.textClass).toContain('warning')
  })
  it('unknown status falls back to a neutral Unknown, never Ready', () => {
    expect(statusTier('nonsense')).toBe('waiting')
    expect(statusPresentation('nonsense').label).toBe('Unknown')
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

describe('TIER_ACCENT rail/dot opacity hierarchy', () => {
  // The muted rail is deliberate: a rail at full strength would compete with the
  // status dot. Family-only assertions (/neutral/) pass either way, so pin the opacity.
  it('mutes the your-turn rail relative to its dot', () => {
    expect(TIER_ACCENT['your-turn'].rail).toContain('/40')
    expect(TIER_ACCENT['your-turn'].dot).not.toContain('/40')
  })
  it('mutes the waiting rail relative to its dot', () => {
    expect(TIER_ACCENT.waiting.rail).toContain('/40')
    expect(TIER_ACCENT.waiting.dot).not.toContain('/40')
  })
})

describe('TIER_ACCENT pulse gating', () => {
  const PULSING: StatusTier[] = ['needs-you', 'error']
  it.each(Object.keys(TIER_ACCENT) as StatusTier[])('pulses only on action-required tiers: %s', (tier) => {
    expect(TIER_ACCENT[tier].pulse).toBe(PULSING.includes(tier))
  })
})

describe('liveness demotion (spec 002 FR-104)', () => {
  it.each(['in_progress', 'waiting_input', 'idle', 'done', 'queued'])(
    'demotes non-terminal %s when liveness is not fresh', (status) => {
      expect(statusPresentation(status, { liveness: 'unreachable' }).tier).toBe('unreachable')
    })
  it.each(['complete', 'ended'])('never demotes terminal %s', (status) => {
    expect(statusPresentation(status, { liveness: 'unreachable' }).tier).not.toBe('unreachable')
  })
  it('fresh (and absent) liveness changes nothing', () => {
    expect(statusPresentation('in_progress', { liveness: 'fresh' }).tier).toBe('busy')
    expect(statusPresentation('in_progress').tier).toBe('busy')
  })
  it('demotes for stale/dead too, not only unreachable', () => {
    expect(statusPresentation('in_progress', { liveness: 'stale' }).tier).toBe('unreachable')
    expect(statusPresentation('in_progress', { liveness: 'dead' }).tier).toBe('unreachable')
  })
})

describe('unknown status (spec 002 FR-402)', () => {
  it('renders neutral Unknown, never Ready', () => {
    const p = statusPresentation('some_future_status')
    expect(p.label).toBe('Unknown')
    expect(p.tier).toBe('waiting')
  })
})
