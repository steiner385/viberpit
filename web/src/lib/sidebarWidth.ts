/**
 * Persisted width for the shell's list column (`app.list`).
 *
 * The column defaults to its historical 320px and is user-resizable within a
 * sensible band: narrow enough to still show the meta row, wide enough to read
 * long session names, but never so wide it crowds out the main pane.
 *
 * `SIDEBAR_MAX` is not a free parameter: `SessionCard.tsx` derives its container
 * -query breakpoints from the fact that the column can never exceed 560px, and
 * `SessionCard.layout.test.tsx` asserts the consequence. Changing it silently
 * changes the card layout.
 */
export const SIDEBAR_MIN = 280
export const SIDEBAR_MAX = 560
export const SIDEBAR_DEFAULT = 320
export const SIDEBAR_STORAGE_KEY = 'ccswitch.listWidth'
/** Pre-AppShell key (DEC-11). Read once, then written forward under the new key. */
const LEGACY_STORAGE_KEY = 'ccswitch.sidebarWidth'

/** Clamp to [MIN, MAX] and round; non-finite input falls back to the default. */
export function clampSidebarWidth(px: number): number {
  if (Number.isNaN(px)) return SIDEBAR_DEFAULT
  return Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, px)))
}

/** A stored string as a usable width; corrupt/non-finite input reads as the default. */
function parseWidth(raw: string): number {
  const px = Number(raw)
  if (!Number.isFinite(px)) return SIDEBAR_DEFAULT
  return clampSidebarWidth(px)
}

/**
 * Read the persisted width, clamped; falls back to the default on miss/corrupt.
 *
 * Migration is one-way and runs at most once: the legacy key is read only when
 * the new key is absent, and is removed as soon as its value has been written
 * forward. A corrupt legacy value still clears the key — otherwise the bad value
 * would be re-read (and re-discarded) on every load.
 */
export function loadSidebarWidth(): number {
  try {
    const current = localStorage.getItem(SIDEBAR_STORAGE_KEY)
    if (current !== null) return parseWidth(current)

    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY)
    if (legacy !== null) {
      const px = parseWidth(legacy)
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(px))
      localStorage.removeItem(LEGACY_STORAGE_KEY)
      return px
    }
  } catch {
    // localStorage can throw (private mode, disabled storage) — degrade gracefully.
    return SIDEBAR_DEFAULT
  }
  return SIDEBAR_DEFAULT
}

/** Persist the width (clamped). Swallows storage errors. */
export function saveSidebarWidth(px: number): void {
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(clampSidebarWidth(px)))
  } catch {
    // ignore — persistence is best-effort
  }
}
