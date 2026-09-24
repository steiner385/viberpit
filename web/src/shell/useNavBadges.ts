import { automationTier } from '@/features/delivery/automationTier'
import type { NavBadgeKey, NavDef } from './navDefs'
import { useAccounts, useSessions, useAllPrs, useAutomations, useCapacityBinding, useDecisions, useTriage } from '@/api/hooks'
import { useDeployState } from '@/lib/deployState'
import { inFlightPrCount } from '@/session/prStatus'
import { isBackgroundSession } from '@/session/backgroundSessions'
import { pendingProposals } from '@/features/roadmap/inbox/triageSelectors'

export type NavBadges = Record<NavBadgeKey, number | boolean | undefined>

/**
 * The counts the deleted `buildTabs(counts)` computed, in one place.
 *
 * DEC-4: `sessions` counts EVERY session in the Active scope, including queued
 * and pending-approval rows, because those render inside the Sessions section
 * too. DesktopShell used `orderedSessions.length` (queued excluded) — that was
 * an artifact of it never rendering PendingApprovalSection. Quiet maintenance
 * sessions are NOT counted: they live in the Background scope precisely so
 * they do not draw the eye, and a nav badge is the eye-drawing surface. One
 * that needs a human lifts into Active and is counted then. The Delivery dot
 * also lights while the capacity readout names a binding constraint (spec
 * 2026-09-20 C8).
 *
 * A zero count comes back as `undefined`, not `0`, so no badge renders.
 * `settings` is boolean-or-undefined — an amber attention dot, never a count.
 *
 * There is no `archived` badge: Archived is a scope of the Sessions list
 * (the segmented control in `ScopeSegment`), not its own nav section, and its
 * count lives on that switch (`SessionListColumn` reads `useArchivedSessions()`
 * directly) rather than here.
 */
export function useNavBadges(): NavBadges {
  const { data: sessions = [] } = useSessions()
  const { data: prs = [] } = useAllPrs()
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

  const automationActions = automationsData.reduce((n, a) => n + (a.actionableCount ?? 0), 0)
  const automationAttention = automationsError || automationsData.some((a) => automationTier(a) === 'needs-you')
  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin' || a.status === 'org-denied',
  )

  return {
    sessions: sessions.filter((s) => !isBackgroundSession(s)).length || undefined,
    pipeline: inFlightPrCount(prs) || undefined,
    // Shared list snapshot; an unknown observation gets a dot, never a fabricated count.
    delivery: automationActions || automationAttention || Boolean(capacity?.binding) || undefined,
    // Proposals awaiting a decision — the count the deleted Triage nav row wore.
    // The queue is the Roadmap section's Inbox scope now (finish-line row 3), so
    // the badge follows it onto Roadmap rather than disappearing with the row.
    // `pendingProposals` because the wire list is widened with the triage loop's
    // fresh auto-accepts (their undo window); those are decided, not backlog.
    // BOTH inbox kinds go through that ONE predicate: a decision proposal is a
    // thing awaiting a human exactly as a classification proposal is, and a
    // badge that counted only one of them would send you to a queue longer than
    // it said.
    roadmap: pendingProposals([...proposals, ...decisions]).length || undefined,
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
 * A number is a count; `true` is an attention flag; `undefined` is nothing. That
 * mapping is the badge convention itself, so it lives beside the badges rather
 * than being restated by each region — `NavRail` and `NavBar` both call this,
 * and the `More` control sums the sections it hides through it too.
 */
export function navBadgeProps(def: NavDef, badges: NavBadges): { count?: number; attention?: boolean } {
  const v = def.badge ? badges[def.badge] : undefined
  return typeof v === 'number' ? { count: v } : { attention: v === true }
}
