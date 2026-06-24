import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Session } from '@/api/types'
import { orderSessions, buildNavItems, type GroupBy, type SortBy, type SessionGroup, type NavItem } from './ordering'
import { loadSessionOrder, saveSessionOrder } from '@/lib/sessionOrder'
import { loadGroupCollapse, saveGroupCollapse } from '@/lib/groupCollapse'

export interface SessionOrdering {
  groupBy: GroupBy
  sortBy: SortBy
  setGroupBy: (g: GroupBy) => void
  setSortBy: (s: SortBy) => void
  groups: SessionGroup[]
  orderedSessions: Session[]
  navItems: NavItem[]
  collapsedKeys: Set<string>
  toggleCollapse: (key: string) => void
  expandGroup: (key: string) => void
  collapseGroup: (key: string) => void
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

  const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(loadGroupCollapse)

  useEffect(() => {
    saveGroupCollapse(collapsedKeys)
  }, [collapsedKeys])

  const toggleCollapse = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const expandGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (!prev.has(key)) return prev
      const next = new Set(prev)
      next.delete(key)
      return next
    })
  }, [])

  const collapseGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (prev.has(key)) return prev
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }, [])

  const filtered = useMemo(
    () => (matchedIds ? sessions.filter((s) => matchedIds.has(s.id)) : sessions),
    [sessions, matchedIds],
  )

  const groups = useMemo(() => orderSessions(filtered, groupBy, sortBy), [filtered, groupBy, sortBy])
  const orderedSessions = useMemo(
    () => groups.filter((g) => !collapsedKeys.has(g.key)).flatMap((g) => g.sessions),
    [groups, collapsedKeys],
  )

  const navItems = useMemo(() => buildNavItems(groups, collapsedKeys), [groups, collapsedKeys])

  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, navItems, collapsedKeys, toggleCollapse, expandGroup, collapseGroup }
}
