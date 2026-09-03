import type { Session } from '@/api/types'
import {
  statusPresentation, TIER_RANK, PR_CHECKS_FAILING,
  type StatusTier, type StatusPresentation,
} from './statusTier'
import { anyChecksFailing } from './prStatus'

/** Tiers at which nothing is already watching the session's PR: it has no live
 *  turn and no armed Monitor/wakeup. Only these get lifted by a failing PR.
 *  `waiting` (waiting_external — an armed Monitor/wakeup, see background_tasks.is_parked)
 *  and `busy` (a live turn) both mean an agent is already on the CI, so a red
 *  rollup there is that agent's job, not the user's — don't escalate to "Needs
 *  attention" and contradict the card's own "Waiting…"/"Working…" pill. */
const AT_REST = new Set<StatusTier>(['your-turn'])

/**
 * The ONE triage read for a session: the presentation its card pill shows AND
 * the tier its group header / sort rank use — computed together so they can
 * never disagree (the reported "Waiting…" pill under a "Needs attention" header).
 *
 * Starts from the session's own status presentation (honoring the restart-strand
 * relabel), then lifts an at-rest session into {@link PR_CHECKS_FAILING} when a
 * still-actionable PR's checks are red. A `waiting`/`busy`/`needs-you` session is
 * never overridden — either an agent is already on the PR, or the session is
 * already more urgent than a failing PR.
 */
export function sessionTriage(session: Session): StatusPresentation {
  const base = statusPresentation(session.status, {
    interruptedByRestart: session.interruptedByRestart,
    liveness: session.liveness,
  })
  if (
    AT_REST.has(base.tier)
    && anyChecksFailing(session.prs)
    && TIER_RANK.error < TIER_RANK[base.tier]
  ) {
    return PR_CHECKS_FAILING
  }
  return base
}

/** Triage tier for grouping/sorting — the tier of the single {@link sessionTriage}
 *  read, so the group a card lands in always matches the pill it shows. */
export function triageTier(session: Session): StatusTier {
  return sessionTriage(session).tier
}

export function triageRank(session: Session): number {
  return TIER_RANK[triageTier(session)]
}
