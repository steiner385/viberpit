// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { ROADMAP_TIERS } from '@/api/roadmap'
import { TIER_TONE, TIER_ACCENT, TIER_TEXT, TIER_RANK, tierLabel, maxTier } from './roadmapTier'

describe('roadmapTier', () => {
  it('covers every server tier exactly once', () => {
    for (const t of ROADMAP_TIERS) {
      expect(TIER_TONE[t]).toBeDefined()
      expect(TIER_ACCENT[t].rail).toMatch(/^bg-/)
      expect(TIER_RANK[t]).toBe(ROADMAP_TIERS.indexOf(t))
      expect(tierLabel(t).length).toBeGreaterThan(2)
      expect(TIER_TEXT[t]).toMatch(/^text-/)
    }
  })
  it('only the two tiers a human must act on are coloured', () => {
    expect(ROADMAP_TIERS.filter((t) => TIER_TEXT[t] !== 'text-ink-muted')).toEqual(['needs-you', 'blocked'])
  })
  it('only needs-you pulses', () => {
    expect(ROADMAP_TIERS.filter((t) => TIER_ACCENT[t].pulse)).toEqual(['needs-you'])
  })
  it('maxTier picks the most urgent and treats unknown as idle', () => {
    expect(maxTier('idle', 'live', 'blocked', 'proposed')).toBe('blocked')
    expect(maxTier()).toBe('idle')
    expect(maxTier('bogus' as never, 'live')).toBe('live')
  })
})
