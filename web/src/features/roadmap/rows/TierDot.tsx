import type { RoadmapTier } from '@/api/roadmap'
import { cn } from '@/lib/utils'
import { TIER_ACCENT } from '../roadmapTier'

/** The row's tier, as a dot. Decorative: every row states its tier in words on
 *  line 2, so the dot is `aria-hidden` rather than a second announcement.
 *
 *  `motion-safe:` — the pulse is gated on `prefers-reduced-motion`, the same
 *  way the sessions card's dot and `StatusPill` are. A bare `animate-pulse`
 *  here survived the emulation on 62 rows at once (2026-09-30 Roadmap
 *  critique, P3): one dot pulsing is a signal, a tree of them is motion. */
export function TierDot({ tier }: { tier: RoadmapTier }) {
  const a = TIER_ACCENT[tier]
  return (
    <span
      aria-hidden="true"
      className={cn('inline-block size-2 shrink-0 rounded-pill', a.dot, a.pulse && 'motion-safe:animate-pulse')}
    />
  )
}
