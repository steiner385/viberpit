/**
 * Persists which session-list groups are collapsed, namespaced per list. Stores
 * the set of COLLAPSED group keys (empty/absent ⇒ all expanded). Namespace 'live'
 * maps to the original key so existing collapse state survives.
 */
const keyFor = (ns: string): string =>
  ns === 'live' ? 'ccswitch.groupCollapse' : `ccswitch.groupCollapse.${ns}`

export function loadGroupCollapseNs(ns: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(keyFor(ns))
    if (!raw) return new Set()
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((k): k is string => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

export function saveGroupCollapseNs(ns: string, keys: Set<string>): void {
  try {
    window.localStorage.setItem(keyFor(ns), JSON.stringify([...keys]))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

// ── Live wrappers (unchanged public API) ─────────────────────────────────────
export function loadGroupCollapse(): Set<string> {
  return loadGroupCollapseNs('live')
}
export function saveGroupCollapse(keys: Set<string>): void {
  saveGroupCollapseNs('live', keys)
}
