/**
 * Persists the dashboard session-list group/sort selection. Mirrors lastRepo.ts:
 * all access is try/catch-wrapped so a blocked/unavailable localStorage degrades
 * to the defaults instead of crashing. Unknown enum values (e.g. from a future
 * build) also fall back to defaults.
 */
import type { GroupBy, SortBy } from '@/session/ordering'

const KEY = 'ccswitch.sessionOrder'
const GROUP_BY: GroupBy[] = ['none', 'repo', 'status']
const SORT_BY: SortBy[] = ['status', 'lastActive', 'age', 'name']

export interface SessionOrderPref {
  groupBy: GroupBy
  sortBy: SortBy
}

const DEFAULT: SessionOrderPref = { groupBy: 'none', sortBy: 'status' }

export function loadSessionOrder(): SessionOrderPref {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return { ...DEFAULT }
    const parsed = JSON.parse(raw) as Partial<SessionOrderPref>
    return {
      groupBy: GROUP_BY.includes(parsed.groupBy as GroupBy) ? (parsed.groupBy as GroupBy) : DEFAULT.groupBy,
      sortBy: SORT_BY.includes(parsed.sortBy as SortBy) ? (parsed.sortBy as SortBy) : DEFAULT.sortBy,
    }
  } catch {
    return { ...DEFAULT }
  }
}

export function saveSessionOrder(pref: SessionOrderPref): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(pref))
  } catch {
    /* ignore — persistence is best-effort */
  }
}
