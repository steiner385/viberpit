/**
 * Persisted width for the DesktopShell sidebar.
 *
 * The sidebar defaults to its historical 320px and is user-resizable within a
 * sensible band: narrow enough to still show the meta row, wide enough to read
 * long session names, but never so wide it crowds out the main pane.
 */
export const SIDEBAR_MIN = 280
export const SIDEBAR_MAX = 560
export const SIDEBAR_DEFAULT = 320
export const SIDEBAR_STORAGE_KEY = 'ccswitch.sidebarWidth'

/** Clamp to [MIN, MAX] and round; non-finite input falls back to the default. */
export function clampSidebarWidth(px: number): number {
  if (Number.isNaN(px)) return SIDEBAR_DEFAULT
  return Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, px)))
}

/** Read the persisted width, clamped; falls back to the default on miss/corrupt. */
export function loadSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_STORAGE_KEY)
    if (raw === null) return SIDEBAR_DEFAULT
    const px = Number(raw)
    if (!Number.isFinite(px)) return SIDEBAR_DEFAULT
    return clampSidebarWidth(px)
  } catch {
    // localStorage can throw (private mode, disabled storage) — degrade gracefully.
    return SIDEBAR_DEFAULT
  }
}

/** Persist the width (clamped). Swallows storage errors. */
export function saveSidebarWidth(px: number): void {
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(clampSidebarWidth(px)))
  } catch {
    // ignore — persistence is best-effort
  }
}
