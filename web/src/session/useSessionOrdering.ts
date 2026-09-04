import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Session } from '@/api/types'
import {
  orderItems, buildNavItems, LIVE_ORDERING,
  type GroupBy, type SortBy, type SessionGroup, type NavItem, type OrderingConfig,
} from './ordering'
import { loadOrderPref, saveOrderPref } from '@/lib/sessionOrder'
import { loadGroupCollapseNs, saveGroupCollapseNs } from '@/lib/groupCollapse'

export interface OrderingSpec<G extends string, S extends string, T> {
  namespace: string
  config: OrderingConfig<T>
  groupByValues: readonly G[]
  sortByValues: readonly S[]
  defaultPref: { groupBy: G; sortBy: S }
}

export interface OrderingHandle<G extends string, S extends string, T> {
  groupBy: G
  sortBy: S
  setGroupBy: (g: G) => void
  setSortBy: (s: S) => void
  groups: SessionGroup<T>[]
  orderedSessions: T[]
  /** Full display order ignoring collapse, search AND list filters — see
   *  nextSessionAfterClose. */
  allOrderedSessions: T[]
  navItems: NavItem<T>[]
  collapsedKeys: Set<string>
  toggleCollapse: (key: string) => void
  expandGroup: (key: string) => void
  collapseGroup: (key: string) => void
}

/**
 * Generic group/sort/collapse state for a list, persisted under spec.namespace.
 * `matchedIds` (when non-null) filters to those item ids (search integration).
 *
 * `allItems` is the SUPERSET `allOrderedSessions` is built from, and defaults to
 * `items`. Search hides rows without removing them from `items`, so it needs no
 * such escape hatch; the Status/Project/Environment filters (DEC-8) do, because
 * they are applied BEFORE this hook — when groups are provided, `SessionList`
 * renders the groups and ignores its `sessions` prop, so filtering afterwards
 * would leave the rows in the grouped list. Passing the pre-filter list here is
 * what keeps the close/archive fallback (`nextSessionAfterClose`'s SECOND
 * argument) whole: filters narrow the visible list, never the fallback, so
 * archiving the last match advances somewhere instead of dead-ending.
 */
export function useItemOrdering<G extends string, S extends string, T>(
  items: readonly T[],
  spec: OrderingSpec<G, S, T>,
  matchedIds: Set<string> | null = null,
  allItems: readonly T[] = items,
): OrderingHandle<G, S, T> {
  const { namespace, config, groupByValues, sortByValues, defaultPref } = spec
  const [groupBy, setGroupBy] = useState<G>(
    () => loadOrderPref(namespace, groupByValues, sortByValues, defaultPref).groupBy,
  )
  const [sortBy, setSortBy] = useState<S>(
    () => loadOrderPref(namespace, groupByValues, sortByValues, defaultPref).sortBy,
  )
  useEffect(() => {
    saveOrderPref(namespace, { groupBy, sortBy })
  }, [namespace, groupBy, sortBy])

  const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(() => loadGroupCollapseNs(namespace))
  useEffect(() => {
    saveGroupCollapseNs(namespace, collapsedKeys)
  }, [namespace, collapsedKeys])

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
    () => (matchedIds ? items.filter((it) => matchedIds.has(config.idOf(it))) : items),
    [items, matchedIds, config],
  )
  const groups = useMemo(
    () => orderItems(filtered, groupBy, sortBy, config),
    [filtered, groupBy, sortBy, config],
  )
  const orderedSessions = useMemo(
    () => groups.filter((g) => !collapsedKeys.has(g.key)).flatMap((g) => g.sessions),
    [groups, collapsedKeys],
  )
  // Same order the user sees, but with collapse, search and list filters ignored
  // — the advance target after archiving an open session must exist even when
  // that session is hidden from the visible list (collapsed group / active
  // search / a filter it does not match).
  const allOrderedSessions = useMemo(
    () => orderItems(allItems, groupBy, sortBy, config).flatMap((g) => g.sessions),
    [allItems, groupBy, sortBy, config],
  )
  const navItems = useMemo(() => buildNavItems(groups, collapsedKeys), [groups, collapsedKeys])

  return {
    groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, allOrderedSessions, navItems,
    collapsedKeys, toggleCollapse, expandGroup, collapseGroup,
  }
}

// ── Live wrapper (unchanged public API) ──────────────────────────────────────
export type SessionOrdering = OrderingHandle<GroupBy, SortBy, Session>

export const LIVE_SPEC: OrderingSpec<GroupBy, SortBy, Session> = {
  namespace: 'live',
  config: LIVE_ORDERING,
  groupByValues: ['none', 'repo', 'status'],
  sortByValues: ['status', 'lastActive', 'age', 'name'],
  defaultPref: { groupBy: 'none', sortBy: 'status' },
}

export function useSessionOrdering(
  sessions: readonly Session[],
  matchedIds: Set<string> | null = null,
  allSessions: readonly Session[] = sessions,
): SessionOrdering {
  return useItemOrdering(sessions, LIVE_SPEC, matchedIds, allSessions)
}
