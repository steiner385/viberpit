import { isSnoozed, type Session } from '@/api/types'
import {
  statusPresentation, TIER_RANK, PR_CHECKS_FAILING, OWNERSHIP_STALE_REASON,
  detachedDeliveredPresentation, detachedPresentation,
  type StatusTier, type StatusPresentation,
} from './statusTier'
import { anyChecksFailing, deliveredPr, isActionablePrState } from './prStatus'
import type { PromptStage } from './promptStage'

/** Tiers at which nothing is already watching the session's PR: it has no live
 *  turn and no armed Monitor/wakeup. Only these get lifted by a failing PR.
 *  `complete` counts: the agent declared the objective met and left, so a red
 *  rollup on its still-open PR is nobody's job but the user's.
 *  `waiting` (waiting_external — an armed Monitor/wakeup, see background_tasks.is_parked)
 *  and `busy` (a live turn) both mean an agent is already on the CI, so a red
 *  rollup there is that agent's job, not the user's — don't escalate to "Failed"
 *  and contradict the card's own "Waiting…"/"Working…" pill. */
const AT_REST = new Set<StatusTier>(['your-turn', 'complete'])

/**
 * The ONE triage read for a session: the presentation its card pill shows AND
 * the tier its group header / sort rank use — computed together so they can
 * never disagree (the reported "Waiting…" pill under a "Failed" header).
 *
 * Starts from the session's own status presentation (honoring the restart-strand
 * relabel), then lifts an at-rest session into {@link PR_CHECKS_FAILING} when a
 * still-actionable PR's checks are red. A `waiting`/`busy`/`needs-you` session is
 * never overridden — either an agent is already on the PR, or the session is
 * already more urgent than a failing PR.
 *
 * A detached session (its automation lost ownership, #3459) reads "Detached — work
 * delivered" over ANY status once its PR is merged or armed, and plain "Detached"
 * at rest; a `needs_attention` one with nothing delivered keeps "Turn failed".
 */
export function sessionTriage(session: Session, promptStage?: PromptStage): StatusPresentation {
  const base = statusPresentation(session.status, {
    interruptedByRestart: session.interruptedByRestart,
    liveness: session.liveness,
    usageStalled: session.usageStalled,
    pausedUntil: session.pausedUntil,
    promptStage,
  })
  const detached = session.pausedReason === OWNERSHIP_STALE_REASON
  // Detached with a merged or armed PR: the work left the session, whatever status
  // the fence caught it in — including a pre-#3456 `needs_attention` (#3459).
  const delivered = detached ? deliveredPr(session.prs) : undefined
  if (delivered) return detachedDeliveredPresentation(delivered.number)
  if (
    AT_REST.has(base.tier)
    && anyChecksFailing(session.prs)
    && TIER_RANK.error < TIER_RANK[base.tier]
  ) {
    return PR_CHECKS_FAILING
  }
  // Detached at a finished turn with nothing delivered yet. A `needs_attention`
  // row keeps "Turn failed": that turn may really have errored.
  if (detached && AT_REST.has(base.tier)) {
    return detachedPresentation(session.prs?.find((pr) => isActionablePrState(pr.state))?.number)
  }
  return base
}

/**
 * Triage tier for grouping/sorting. Deliberately called WITHOUT a prompt stage: the
 * in-flight overlay maps to the `busy` tier, which sorts last, so letting a send drive
 * grouping would fling the card to the bottom of the list the moment the user hit
 * Enter and fling it back when the turn started. The pill carries that news instead —
 * the one place the pill/header pairing this module guarantees is intentionally
 * relaxed, because the alternative moves the thing the user is looking at.
 *
 * Otherwise: the tier of the single {@link sessionTriage}
 *  read, so the group a card lands in always matches the pill it shows. */
export function triageTier(session: Session): StatusTier {
  return sessionTriage(session).tier
}

export function triageRank(session: Session): number {
  return TIER_RANK[triageTier(session)]
}

/**
 * How many of these sessions are waiting on a human — the `needs-you` tier of
 * the same triage read the card pill, group header and sort use. THE queue
 * number (PRODUCT.md principle 1): the nav badge and the Active scope's
 * attention dot both read it, so they can never disagree with the list.
 * A snoozed session is out of the list and must not nag, so it never counts.
 */
export function needsYouCount(sessions: readonly Session[]): number {
  return sessions.reduce((n, s) => n + (!isSnoozed(s) && triageTier(s) === 'needs-you' ? 1 : 0), 0)
}
