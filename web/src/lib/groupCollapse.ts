/**
 * Persists which session-list groups are collapsed. Mirrors sessionOrder.ts:
 * all access is try/catch-wrapped so a blocked/unavailable localStorage degrades
 * to "nothing collapsed" instead of crashing. We store the set of COLLAPSED group
 * keys (so an empty/absent value means everything is expanded, and any newly
 * appearing group defaults to expanded). Keys are the stable group.key produced by
 * orderSessions (status tier name or repo label).
 */
const KEY = 'ccswitch.groupCollapse'

export function loadGroupCollapse(): Set<string> {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((k): k is string => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

export function saveGroupCollapse(keys: Set<string>): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify([...keys]))
  } catch {
    /* ignore — persistence is best-effort */
  }
}
