/**
 * An automation's attention tier (finish-line spec §4.4), computed from the
 * list payload plus its actionable-item count. The vocabulary is the roadmap's
 * (`RoadmapTier`) so the row's rail, dot and pill reuse `roadmapTier.ts` and
 * `TIER_LABEL` rather than a second table.
 *
 * `blocked` reads the loop's `warn` level: the loop reports `warn` while paused
 * outside its active hours and while degraded, which is the payload-carried
 * signal closest to "its schedule window is closed" (ruling R2).
 */
import type { Automation } from '@/api/automations'
import type { RoadmapTier } from '@/api/roadmap'
import { TIER_LABEL } from '@/features/products/vocab'
import { TIER_RANK } from '@/features/roadmap/roadmapTier'

export type AutomationTier = Extract<RoadmapTier, 'needs-you' | 'blocked' | 'in-flight' | 'idle'>

type TierInput = Pick<Automation, 'enabled' | 'status' | 'activeSessions'>

export function automationTier(a: TierInput, actionable: number): AutomationTier {
  if (actionable > 0 || a.status.level === 'error') return 'needs-you'
  if (a.enabled && a.status.level === 'warn') return 'blocked'
  if (a.activeSessions > 0 || a.status.level === 'active') return 'in-flight'
  return 'idle'
}

export const AUTOMATION_TIER_RANK: Record<AutomationTier, number> = {
  'needs-you': TIER_RANK['needs-you'], blocked: TIER_RANK.blocked, 'in-flight': TIER_RANK['in-flight'], idle: TIER_RANK.idle,
}

/** Line 2 of an automation row: the tier word plus the one number it rests on. */
export function automationTierLine(tier: AutomationTier, a: TierInput, actionable: number): string {
  const word = TIER_LABEL[tier]
  if (tier === 'needs-you' && actionable > 0) return `${word} · ${actionable} actionable`
  if (tier === 'in-flight' && a.activeSessions > 0) {
    return `${word} · ${a.activeSessions} live ${a.activeSessions === 1 ? 'session' : 'sessions'}`
  }
  return word
}
