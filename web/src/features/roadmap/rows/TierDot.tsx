import type { RoadmapTier } from '@/api/roadmap'
import { cn } from '@/lib/utils'
import { TIER_ACCENT } from '../roadmapTier'

/** The row's tier, as a dot. Decorative: every row states its tier in words on
 *  line 2, so the dot is `aria-hidden` rather than a second announcement. */
export function TierDot({ tier }: { tier: RoadmapTier }) {
  const a = TIER_ACCENT[tier]
  return (
    <span
      aria-hidden="true"
      className={cn('inline-block size-2 shrink-0 rounded-pill', a.dot, a.pulse && 'animate-pulse')}
    />
  )
}
