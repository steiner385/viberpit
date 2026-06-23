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
 */
export function useSessionOrdering(sessions: readonly Session[]): SessionOrdering {
  const initial = loadSessionOrder()
  const [groupBy, setGroupBy] = useState<GroupBy>(initial.groupBy)
  const [sortBy, setSortBy] = useState<SortBy>(initial.sortBy)

  useEffect(() => {
    saveSessionOrder({ groupBy, sortBy })
  }, [groupBy, sortBy])

  const groups = useMemo(() => orderSessions(sessions, groupBy, sortBy), [sessions, groupBy, sortBy])
  const orderedSessions = useMemo(() => groups.flatMap((g) => g.sessions), [groups])

  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions }
}
