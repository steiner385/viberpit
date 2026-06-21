/**
 * Tiny persistence helper for the dispatch bar's last-chosen repo.
 * Stores a GitHub `nameWithOwner` string. Net-new — nothing persisted this
 * value before. All access is wrapped so a blocked / unavailable localStorage
 * (private mode, quota, SSR) degrades to "no remembered repo" instead of crashing.
 */
const KEY = 'ccswitch.lastRepo'

export function getLastRepo(): string | null {
  try {
    return window.localStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function setLastRepo(nameWithOwner: string): void {
  try {
    window.localStorage.setItem(KEY, nameWithOwner)
  } catch {
    /* ignore — persistence is best-effort */
  }
}
