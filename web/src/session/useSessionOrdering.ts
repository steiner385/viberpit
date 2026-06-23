import { useEffect, useMemo, useState } from 'react'
import type { Session } from '@/api/types'
import { orderSessions, type GroupBy, type SortBy, type SessionGroup } from './ordering'
import { loadSessionOrder, saveSessionOrder } from '@/lib/sessionOrder'

export interface SessionOrdering {
  groupBy: GroupBy
  sortBy: SortBy
  setGroupBy: (g: GroupBy) => void
  setSortBy: (s: SortBy) => void
  groups: SessionGroup[]
  orderedSessions: Session[]
}

/**
 * Shared group/sort state for the session list, used by both layouts
 * (DesktopShell ≥1024px and Dashboard <1024px) so ordering stays consistent and
 * the logic lives in one place. State is restored from / persisted to
 * localStorage.
 *
 * @param matchedIds - When non-null, only sessions whose id is in the set are
 *   included in groups and orderedSessions. Pass null (or omit) for no filter.
 */
export function useSessionOrdering(
  sessions: readonly Session[],
  matchedIds: Set<string> | null = null,
): SessionOrdering {
  const [groupBy, setGroupBy] = useState<GroupBy>(() => loadSessionOrder().groupBy)
  const [sortBy, setSortBy] = useState<SortBy>(() => loadSessionOrder().sortBy)

  useEffect(() => {
    saveSessionOrder({ groupBy, sortBy })
  }, [groupBy, sortBy])

  const filtered = useMemo(
    () => (matchedIds ? sessions.filter((s) => matchedIds.has(s.id)) : sessions),
    [sessions, matchedIds],
  )

  const groups = useMemo(() => orderSessions(filtered, groupBy, sortBy), [filtered, groupBy, sortBy])
  const orderedSessions = useMemo(() => groups.flatMap((g) => g.sessions), [groups])

  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions }
}
