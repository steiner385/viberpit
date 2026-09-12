/**
 * Tier → tone. The TIER itself is computed server-side, once, in
 * `server/pm/tree.py`; this module is the web's only knowledge of what a tier
 * looks like, shaped like `session/statusTier.ts` so rows and pills read alike.
 */
import { ROADMAP_TIERS, type RoadmapTier } from '@/api/roadmap'
import type { Tone } from '@/ui/Badge'
import { TIER_LABEL } from '@/features/products/vocab'

export const TIER_TONE: Record<RoadmapTier, Tone> = {
  'needs-you': 'warning',
  blocked: 'danger',
  'in-flight': 'success',
  proposed: 'info',
  live: 'neutral',
  idle: 'neutral',
}

/** Text colour for a tier's inline reason line. Beside `TIER_TONE` so a row and
 *  a list cannot disagree about what colour "blocked" reads in — the two
 *  surfaces that show a reason (`InitiativeCard`, `NeedsYouList`) both read it. */
export const TIER_TEXT: Record<RoadmapTier, string> = {
  'needs-you': 'text-warning',
  blocked: 'text-danger',
  'in-flight': 'text-ink-muted',
  proposed: 'text-ink-muted',
  live: 'text-ink-muted',
  idle: 'text-ink-muted',
}

/** Rail + dot classes, muted where the tier is calm (same reading as sessions). */
export const TIER_ACCENT: Record<RoadmapTier, { rail: string; dot: string; pulse: boolean }> = {
  'needs-you': { rail: 'bg-warning', dot: 'bg-warning', pulse: true },
  blocked: { rail: 'bg-danger', dot: 'bg-danger', pulse: false },
  'in-flight': { rail: 'bg-success', dot: 'bg-success', pulse: false },
  proposed: { rail: 'bg-info/40', dot: 'bg-info', pulse: false },
  live: { rail: 'bg-neutral/40', dot: 'bg-neutral', pulse: false },
  idle: { rail: 'bg-neutral/20', dot: 'bg-neutral/60', pulse: false },
}

/** Moving or stuck — everything a human would call "open work" on a node.
 *  `live`, `proposed` and `idle` are history or not started yet. Every detail
 *  level filters its in-flight list with THIS set, so a product and one of its
 *  capabilities cannot disagree about what "in flight" means. */
export const IN_FLIGHT_TIERS: ReadonlySet<RoadmapTier> = new Set<RoadmapTier>([
  'in-flight', 'blocked', 'needs-you',
])

export const TIER_RANK: Record<RoadmapTier, number> = Object.fromEntries(
  ROADMAP_TIERS.map((t, i) => [t, i]),
) as Record<RoadmapTier, number>

export const tierLabel = (tier: RoadmapTier): string => TIER_LABEL[tier]

/** The most urgent of `tiers`; unknown strings count as idle (mirrors `max_tier`). */
export function maxTier(...tiers: RoadmapTier[]): RoadmapTier {
  let best: RoadmapTier = 'idle'
  for (const t of tiers) {
    const r = TIER_RANK[t]
    if (r !== undefined && r < TIER_RANK[best]) best = t
  }
  return best
}
