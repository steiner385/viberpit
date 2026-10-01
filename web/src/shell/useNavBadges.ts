import { automationTier } from '@/features/delivery/automationTier'
import type { NavBadgeKey, NavDef } from './navDefs'
import { useAccounts, useSessions, useAutomations, useCapacityBinding, useDecisions, useTriage } from '@/api/hooks'
import { useDeployState } from '@/lib/deployState'
import { isBackgroundSession } from '@/session/backgroundSessions'
import { needsYouCount } from '@/session/triageTier'
import { pendingProposals } from '@/features/roadmap/inbox/triageSelectors'

/** A count that is ALSO a call for attention: a queue waiting on a human.
 *  Renders as an amber count, and announces "(n, needs attention)". */
export interface AttentionCount { count: number; attention: true }

export type NavBadges = Record<NavBadgeKey, boolean | AttentionCount | undefined>

/**
 * The badges the rail, the phone bar and the More sheet wear, in one place.
 *
 * ## The one rule
 *
 * A nav badge is an amber needs-you count, an amber attention dot, or nothing.
 * It never counts inventory. The 2026-10-01 shell critique measured what four
 * conventions in one pill cost: Sessions wore its queue (amber), Roadmap its
 * queue (but neutral), Pipeline the in-flight PR inventory (19), Delivery a
 * summed actionable count (100 live) plus a dot — and on the glance surface the
 * operator could not tell queue from inventory except by wash, with the one
 * non-Sessions queue painted as inventory. PRODUCT.md principle 1 ("the queue
 * is the product") decides it the way it decided Sessions' badge before:
 * the badge counts what needs the operator; each section's scope pills keep
 * the inventory (Active's count, the roadmap's product count, Pipeline's own
 * health header).
 *
 * `sessions` is THE QUEUE: how many Active-scope sessions are waiting on a
 * human (`needsYouCount` — the `needs-you` tier of the same triage read the
 * card pill and group header use). Quiet maintenance sessions are excluded:
 * they live in the Background scope precisely so they do not draw the eye, and
 * one that needs a human lifts into Active and is counted then.
 *
 * `roadmap` is the Inbox queue: proposals and decisions awaiting a human.
 *
 * `delivery` is the decisions the automations are waiting on (the
 * `actionableCount` each row carries, decisions only since #2579), as an amber
 * count; with none, an amber dot while something still wants eyes — an
 * automation at the needs-you or blocked tier (unknown observation, error,
 * refusal, defect), the list failing to load, or the capacity readout naming a
 * binding constraint ("nothing can run", spec 2026-09-20 C8).
 *
 * `settings` is boolean-or-undefined — an amber attention dot, never a count.
 *
 * A zero comes back as `undefined`, not `0`, so no badge renders. There is no
 * `archived` badge: Archived is a scope of the Sessions list, and its count
 * lives on that switch.
 */
export function useNavBadges(): NavBadges {
  const { data: sessions = [] } = useSessions()
  const { data: automationsData = [], isError: automationsError } = useAutomations({ poll: true })
  const { data: accounts = [] } = useAccounts()
  // Deliberately UNSCOPED and ungated, unlike the Roadmap column's copy
  // (`useRoadmapWorkspace`, `enabled: active`). A section badge has to be right
  // while you are looking at another section, so this one 15 s poll runs
  // app-wide; it shares `[...QK.triage, null]` with the Inbox column whenever
  // that column is not narrowed to a product, so react-query dedupes the two and
  // the nav badge and the scope badge can never disagree. This observer OWNS
  // that key's interval: the column passes `poll: false` for the unscoped queue
  // precisely so the two do not run two timers over one key.
  const { data: proposals = [] } = useTriage()
  // The inbox's second card kind (spec 2026-09-18 §6, "notification: reuses the
  // pending-proposal channel, no new kind"). Unscoped and polled for the same
  // reason the triage list above is, and THIS observer owns the key's interval:
  // the Roadmap column passes `poll: false`.
  const { data: decisions = [] } = useDecisions()
  const deploy = useDeployState()
  // C8: a binding capacity constraint is "nothing can run" — visible from any
  // section. Reads the governor's persisted binding via the one-row
  // `/api/capacity/binding`, not the full readout, so polling it app-wide at the
  // governor's own 60 s tick costs no candidates read or per-repo gh snapshot.
  const { data: capacity } = useCapacityBinding()

  const decisionsWaiting = automationsData.reduce((n, a) => n + (a.actionableCount ?? 0), 0)
  // Needs-you covers an unknown observation, an error and a human wait;
  // blocked is a warn with no typed wait — a refusal or a defect (#2583).
  const automationAttention = automationsError
    || automationsData.some((a) => { const t = automationTier(a); return t === 'needs-you' || t === 'blocked' })
  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin' || a.status === 'org-denied',
  )

  const queue = needsYouCount(sessions.filter((s) => !isBackgroundSession(s)))
  const inbox = pendingProposals([...proposals, ...decisions]).length
  return {
    sessions: queue > 0 ? { count: queue, attention: true } : undefined,
    // `pendingProposals` because the wire list is widened with the triage loop's
    // fresh auto-accepts (their undo window); those are decided, not backlog.
    // BOTH inbox kinds go through that ONE predicate: a decision proposal is a
    // thing awaiting a human exactly as a classification proposal is, and a
    // badge that counted only one of them would send you to a queue longer than
    // it said.
    roadmap: inbox > 0 ? { count: inbox, attention: true } : undefined,
    delivery: decisionsWaiting > 0
      ? { count: decisionsWaiting, attention: true }
      : (automationAttention || Boolean(capacity?.binding) || undefined),
    // A FAILED deploy, not a pending one. The deploy banner is hidden on phone
    // (`useAppBanners`, `hideOn: ['phone']`), so without this dot a lost session
    // would be silent there — Settings › Deploy has the detail but nothing would
    // send you to it. Pending commits are deliberately excluded: master advances
    // all day, and a dot that is always lit is not a signal.
    settings: hasNeedsReauth || deploy.hasFailure || undefined,
  }
}

/**
 * How a section's badge value becomes `NavItem` props.
 *
 * `true` is an attention flag; an {@link AttentionCount} is an amber count;
 * `undefined` is nothing. That mapping is the badge convention itself, so it
 * lives beside the badges rather than being restated by each region —
 * `NavRail` and `NavBar` both call this, and the `More` control sums the
 * sections it hides through it too.
 */
export function navBadgeProps(def: NavDef, badges: NavBadges): { count?: number; attention?: boolean } {
  const v = def.badge ? badges[def.badge] : undefined
  if (typeof v === 'object') return { count: v.count, attention: true }
  return { attention: v === true }
}
