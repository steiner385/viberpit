import type { NavBadgeKey, NavDef } from './navDefs'
import { useAccounts, useSessions, useArchivedSessions, useAllPrs, useAutomations } from '@/api/hooks'
import { inFlightPrCount } from '@/session/prStatus'

export type NavBadges = Record<NavBadgeKey, number | boolean | undefined>

/**
 * The counts the deleted `buildTabs(counts)` computed, in one place.
 *
 * DEC-4: `sessions` counts EVERY session in the section, including queued and
 * pending-approval rows, because those render inside the Sessions section too.
 * DesktopShell used `orderedSessions.length` (queued excluded) — that was an
 * artifact of it never rendering PendingApprovalSection.
 *
 * A zero count comes back as `undefined`, not `0`, so no badge renders.
 * `settings` is boolean-or-undefined — an amber attention dot, never a count.
 */
export function useNavBadges(): NavBadges {
  const { data: sessions = [] } = useSessions()
  const { data: archivedSessions = [] } = useArchivedSessions()
  const { data: prs = [] } = useAllPrs()
  const { data: automationsData = [] } = useAutomations()
  const { data: accounts = [] } = useAccounts()

  const automationsActiveSessions = automationsData.reduce((n, a) => n + a.activeSessions, 0)
  const hasNeedsReauth = accounts.some(
    (a) => a.status === 'needs-reauth' || a.status === 'needs-relogin',
  )

  return {
    sessions: sessions.length || undefined,
    pipeline: inFlightPrCount(prs) || undefined,
    automations: automationsActiveSessions || undefined,
    archived: archivedSessions.length || undefined,
    settings: hasNeedsReauth || undefined,
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
