// web/src/features/sessions/useSessionListState.ts
//
// The Sessions list's state, in one place.
//
// Dashboard and DesktopShell each open-coded this wiring inline, and they drifted:
// the partition that decides which rows are held OUT of the grouped list was
// `!isPreSpawnStatus(s.status)` on phone but `s.status !== 'queued'` on desktop, so
// `pending_approval` rows leaked into the grouped list on wide screens — where no
// PendingApprovalSection existed to show them properly either. DEC-3 settles it on
// `isPreSpawnStatus`, and this hook is the single place that applies it.
//
// It is a hook, not a component-private `useMemo`, because the shell above the
// column needs the same handles the column renders from: the prompt bar drives
// `nav` (↑/↓, jump, expand/collapse) and close/archive advance uses
// `ordering.orderedSessions`. One call site, one state — not two copies that can
// disagree about which session is highlighted.
import { useMemo } from 'react'
import { isPreSpawnStatus, type Session } from '@/api/types'
import { useSessionSearch, type SessionSearch } from '@/session/useSessionSearch'
import { useSessionOrdering, type SessionOrdering } from '@/session/useSessionOrdering'
import { useListNavigation, type ListNavigation } from '@/session/useListNavigation'

export interface SessionListState {
  /** Every live session, pre-spawn rows included — what the list-controls gate counts. */
  sessions: readonly Session[]
  /** Waiting for a capacity slot, in queue order (QueuedSection). */
  queued: Session[]
  /** Parked at the launch-time model gate (PendingApprovalSection). */
  pendingApproval: Session[]
  /** Search box state; `matchedIds` already feeds `ordering`. */
  search: SessionSearch
  /** Group/sort/collapse state and the derived groups + navItems. */
  ordering: SessionOrdering
  /** Keyboard cursor over `ordering.navItems`. */
  nav: ListNavigation
}

/**
 * Partition the live sessions (DEC-3) and derive search → ordering → navigation
 * from the non-pre-spawn remainder.
 *
 * Pre-spawn sessions are rendered by their own sections and must not enter
 * `useSessionOrdering`: when groups are provided, SessionList renders the groups
 * and ignores its `sessions` prop, so filtering after the fact leaves those rows
 * in the grouped list (their status has no tier mapping and falls through to the
 * your-turn bucket).
 */
export function useSessionListState(sessions: readonly Session[]): SessionListState {
  const queued = useMemo(
    () =>
      sessions
        .filter((s) => s.status === 'queued')
        .sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0)),
    [sessions],
  )
  const pendingApproval = useMemo(
    () => sessions.filter((s) => s.status === 'pending_approval'),
    [sessions],
  )
  const listed = useMemo(() => sessions.filter((s) => !isPreSpawnStatus(s.status)), [sessions])

  const search = useSessionSearch()
  const ordering = useSessionOrdering(listed, search.matchedIds)
  const nav = useListNavigation(ordering.navItems)

  return { sessions, queued, pendingApproval, search, ordering, nav }
}
