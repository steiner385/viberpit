/** Visual attention ordering from the same list snapshot used by navigation. */
import type { Automation } from '@/api/automations'
import type { RoadmapTier } from '@/api/roadmap'
import { TIER_LABEL } from '@/features/products/vocab'
import { TIER_RANK } from '@/features/roadmap/roadmapTier'

export type AutomationTier = Extract<RoadmapTier, 'needs-you' | 'in-flight' | 'idle'>
type TierInput = Pick<Automation, 'enabled' | 'status' | 'activeSessions' | 'actionableCount' | 'maintenanceCount'>

export function automationTier(a: TierInput): AutomationTier {
  if ((a.actionableCount ?? 0) > 0 || a.status.level === 'error' || a.status.waitingReason === 'human') return 'needs-you'
  if ((a.enabled || a.activeSessions > 0) && (a.status.level === 'unknown' || a.actionableCount == null)) return 'needs-you'
  if (a.activeSessions > 0 || a.status.level === 'active') return 'in-flight'
  return 'idle'
}

export const AUTOMATION_TIER_RANK: Record<AutomationTier, number> = {
  'needs-you': TIER_RANK['needs-you'], 'in-flight': TIER_RANK['in-flight'], idle: TIER_RANK.idle,
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
  else if (a.status.level === 'warn') parts.push('Notice')
  if (a.activeSessions > 0) parts.push(`${a.activeSessions} live ${a.activeSessions === 1 ? 'session' : 'sessions'}`)
  if (parts.length === 1 && a.actionableCount === 0) parts.unshift(TIER_LABEL[tier])
  return parts.join(' · ')
}
