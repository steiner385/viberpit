/** Visual attention ordering from the same list snapshot used by navigation. */
import type { Automation } from '@/api/automations'
import type { RoadmapTier } from '@/api/roadmap'
import { TIER_LABEL } from '@/features/products/vocab'
import { TIER_RANK } from '@/features/roadmap/roadmapTier'

export type AutomationTier = Extract<RoadmapTier, 'needs-you' | 'blocked' | 'in-flight' | 'idle'>
type TierInput = Pick<Automation, 'enabled' | 'status' | 'activeSessions' | 'actionableCount' | 'maintenanceCount'>

/** A warn-level observation with NO typed waiting reason is the automation
 *  refusing or failing to do its job — "Reconcile refused: master is 9 commits
 *  ahead", "claim held by a missing session — a defect, not load" — not a
 *  pause it chose. Those used to tier idle and read calm beside "Idle"
 *  (2026-10-01 Delivery critique, P2). A warn WITH a reason (schedule,
 *  capacity, budget, policy) is an expected wait and stays idle. */
const isBlocked = (a: TierInput): boolean =>
  a.status.level === 'warn' && !a.status.waitingReason && (a.enabled || a.activeSessions > 0)

export function automationTier(a: TierInput): AutomationTier {
  if ((a.actionableCount ?? 0) > 0 || a.status.level === 'error' || a.status.waitingReason === 'human') return 'needs-you'
  if ((a.enabled || a.activeSessions > 0) && (a.status.level === 'unknown' || a.actionableCount == null)) return 'needs-you'
  if (isBlocked(a)) return 'blocked'
  if (a.activeSessions > 0 || a.status.level === 'active') return 'in-flight'
  return 'idle'
}

export const AUTOMATION_TIER_RANK: Record<AutomationTier, number> = {
  'needs-you': TIER_RANK['needs-you'], blocked: TIER_RANK.blocked, 'in-flight': TIER_RANK['in-flight'], idle: TIER_RANK.idle,
}

/** Keep observation, schedule waits, and activity separate from attention counts. */
export function automationTierLine(tier: AutomationTier, a: TierInput): string {
  const parts: string[] = []
  if (a.status.level === 'unknown') parts.push('Status unknown')
  if (a.actionableCount == null) parts.push('Actionable count unknown')
  else if (a.actionableCount > 0) parts.push(`${a.actionableCount} actionable`)
  else parts.push('0 actionable')
  // Settled housekeeping a human may revisit: said, never tiered or badged
  // (2026-10-01 Delivery critique, P0 — 96 of a 103 badge were these).
  if ((a.maintenanceCount ?? 0) > 0) parts.push(`${a.maintenanceCount} maintenance`)
  if (a.status.waitingReason) parts.push(`Waiting for ${a.status.waitingReason}`)
  // A warn that did not tier blocked (it has actionable items, so it is
  // needs-you; or the loop is off) is still an observation worth a word.
  else if (a.status.level === 'warn' && tier !== 'blocked') parts.push('Notice')
  if (a.activeSessions > 0) parts.push(`${a.activeSessions} live ${a.activeSessions === 1 ? 'session' : 'sessions'}`)
  // Blocked always says so: the red rail is tone, this is the word
  // (Tone-Plus-Word). Every other tier is already named by its count or wait.
  if (tier === 'blocked') parts.unshift(TIER_LABEL.blocked)
  else if (parts.length === 1 && a.actionableCount === 0) parts.unshift(TIER_LABEL[tier])
  return parts.join(' · ')
}
