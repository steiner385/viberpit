/**
 * Persists session-list group/sort selection AND the Status/Project/Environment
 * filters, namespaced per list ('live', 'archived', …). All access is
 * try/catch-wrapped so a blocked localStorage degrades to defaults. Namespace
 * 'live' maps to the original key so existing user prefs survive.
 *
 * Filters live in the SAME stored object as groupBy/sortBy (DEC-8) rather than
 * under a `ccswitch.listFilters` key of their own: one list's state, one key,
 * one module — a third mechanism would fragment it across three. Both writers
 * merge into the stored object, so saving an ordering never drops the filters
 * and saving filters never drops the ordering.
 */
import type { GroupBy, SortBy } from '@/session/ordering'
import {
  coerceFilters, sanitizeFilters,
  type FilterOptions, type ListFilters,
} from '@/session/listFilters'

const keyFor = (ns: string): string =>
  ns === 'live' ? 'ccswitch.sessionOrder' : `ccswitch.sessionOrder.${ns}`

/** The stored object, read as unknown keys — every field is validated on the
 *  way out, so a hand-edited or stale entry can never widen the app's state. */
function readStored(ns: string): Record<string, unknown> {
  try {
    const raw = window.localStorage.getItem(keyFor(ns))
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return {}
    return parsed as Record<string, unknown>
  } catch {
    return {}
  }
}

/** Merge `patch` into the stored object so the two writers cannot clobber
 *  each other's half of the pref. */
function writeMerged(ns: string, patch: Record<string, unknown>): void {
  try {
    window.localStorage.setItem(keyFor(ns), JSON.stringify({ ...readStored(ns), ...patch }))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

export function loadOrderPref<G extends string, S extends string>(
  ns: string,
  validGroup: readonly G[],
  validSort: readonly S[],
  def: { groupBy: G; sortBy: S },
): { groupBy: G; sortBy: S } {
  const parsed = readStored(ns)
  return {
    groupBy: validGroup.includes(parsed.groupBy as G) ? (parsed.groupBy as G) : def.groupBy,
    sortBy: validSort.includes(parsed.sortBy as S) ? (parsed.sortBy as S) : def.sortBy,
  }
}

export function saveOrderPref<G extends string, S extends string>(
  ns: string,
  pref: { groupBy: G; sortBy: S },
): void {
  writeMerged(ns, { groupBy: pref.groupBy, sortBy: pref.sortBy })
}

/**
 * The persisted filters, validated against the values `options` currently
 * offers. A stored value that no longer exists (a retired fleet host, a repo no
 * session uses) falls back to "no filter" — see session/listFilters for why
 * emptying the list instead would be unrecoverable from the UI.
 */
export function loadListFilters(ns: string, options: FilterOptions): ListFilters {
  return sanitizeFilters(coerceFilters(readStored(ns).filters), options)
}

export function saveListFilters(ns: string, filters: ListFilters): void {
  writeMerged(ns, { filters })
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
