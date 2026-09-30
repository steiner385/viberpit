// The tier dot's pulse honours `prefers-reduced-motion` (2026-09-30 Roadmap
// critique, P3: 62 un-gated pulses survived the emulation). Same contract
// `StatusPill.rich.test` pins for the sessions pill.
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ROADMAP_TIERS } from '@/api/roadmap'
import { TierDot } from './TierDot'

describe('TierDot', () => {
  it('pulses the needs-you tier only behind motion-safe', () => {
    const { container } = render(<TierDot tier="needs-you" />)
    const dot = container.firstElementChild!
    expect(dot.className).toContain('motion-safe:animate-pulse')
    expect(dot.className).not.toMatch(/(^|\s)animate-pulse/)
    expect(dot).toHaveAttribute('aria-hidden', 'true')
  })

  it('never animates any other tier', () => {
    for (const tier of ROADMAP_TIERS.filter((t) => t !== 'needs-you')) {
      const { container, unmount } = render(<TierDot tier={tier} />)
      expect(container.firstElementChild!.className).not.toContain('animate-pulse')
      unmount()
    }
  })
})
