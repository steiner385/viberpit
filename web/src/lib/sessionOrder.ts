/**
 * Persists session-list group/sort selection, namespaced per list ('live',
 * 'archived', …). All access is try/catch-wrapped so a blocked localStorage
 * degrades to defaults. Namespace 'live' maps to the original key so existing
 * user prefs survive.
 */
import type { GroupBy, SortBy } from '@/session/ordering'

const keyFor = (ns: string): string =>
  ns === 'live' ? 'ccswitch.sessionOrder' : `ccswitch.sessionOrder.${ns}`

export function loadOrderPref<G extends string, S extends string>(
  ns: string,
  validGroup: readonly G[],
  validSort: readonly S[],
  def: { groupBy: G; sortBy: S },
): { groupBy: G; sortBy: S } {
  try {
    const raw = window.localStorage.getItem(keyFor(ns))
    if (!raw) return { ...def }
    const parsed = JSON.parse(raw) as Partial<{ groupBy: G; sortBy: S }>
    return {
      groupBy: validGroup.includes(parsed.groupBy as G) ? (parsed.groupBy as G) : def.groupBy,
      sortBy: validSort.includes(parsed.sortBy as S) ? (parsed.sortBy as S) : def.sortBy,
    }
  } catch {
    return { ...def }
  }
}

export function saveOrderPref<G extends string, S extends string>(
  ns: string,
  pref: { groupBy: G; sortBy: S },
): void {
  try {
    window.localStorage.setItem(keyFor(ns), JSON.stringify(pref))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

// ── Live wrappers (unchanged public API) ─────────────────────────────────────
export interface SessionOrderPref {
  groupBy: GroupBy
  sortBy: SortBy
}
const LIVE_GROUP: GroupBy[] = ['none', 'repo', 'status']
const LIVE_SORT: SortBy[] = ['status', 'lastActive', 'age', 'name']
const LIVE_DEFAULT: SessionOrderPref = { groupBy: 'none', sortBy: 'status' }

export function loadSessionOrder(): SessionOrderPref {
  return loadOrderPref('live', LIVE_GROUP, LIVE_SORT, LIVE_DEFAULT)
}
export function saveSessionOrder(pref: SessionOrderPref): void {
  saveOrderPref('live', pref)
}
