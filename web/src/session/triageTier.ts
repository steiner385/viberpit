import type { Session } from '@/api/types'
import { statusTier, TIER_RANK, type StatusTier } from './statusTier'
import { anyChecksFailing } from './prStatus'

/** Tiers at which nothing is already watching the session's PR: it has no live
 *  turn and no armed Monitor/wakeup. Only these get lifted by a failing PR.
 *  `waiting` (waiting_external — an armed Monitor/wakeup, see background_tasks.is_parked)
 *  and `busy` (a live turn) both mean an agent is already on the CI, so a red
 *  rollup there is that agent's job, not the user's — don't escalate to "Needs
 *  attention" and contradict the card's own "Waiting…"/"Working…" pill. */
const AT_REST = new Set<StatusTier>(['your-turn'])

/** A broken PR is an attention signal only on an at-rest session — one that has
 *  handed nothing off and isn't mid-turn. */
function prTier(session: Session): StatusTier | null {
  if (!AT_REST.has(statusTier(session.status))) return null
  return anyChecksFailing(session.prs) ? 'error' : null
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
