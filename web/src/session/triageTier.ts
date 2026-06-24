import type { Session } from '@/api/types'
import { statusTier, TIER_RANK, type StatusTier } from './statusTier'

/** A broken PR is an attention signal even on an otherwise-calm session. */
function prTier(session: Session): StatusTier | null {
  return session.prs?.some((pr) => pr.checks === 'failing') ? 'error' : null
}

/** Triage tier = the MORE URGENT (lower TIER_RANK) of the session's status tier
 *  and any PR-derived tier. Reuses the single TIER_RANK source — no parallel rank. */
export function triageTier(session: Session): StatusTier {
  const st = statusTier(session.status)
  const pt = prTier(session)
  if (pt && TIER_RANK[pt] < TIER_RANK[st]) return pt
  return st
}

export function triageRank(session: Session): number {
  return TIER_RANK[triageTier(session)]
}
