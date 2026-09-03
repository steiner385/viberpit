import { useCallback, useEffect, useState } from 'react'

/**
 * Persisted width for the shell's drawer column (`app.drawer`), sub-spec B §4.2.
 *
 * The band is narrower than the list column's: the drawer holds prose (help
 * text today, the assistant transcript from PR 12), so it needs enough width to
 * read a sentence without becoming a second main pane. `DRAWER_DEFAULT` is the
 * `Sheet` primitive's own default, so an unresized drawer looks exactly like
 * every other right-hand sheet in the app.
 *
 * `web/src/lib/` has four bespoke persisted values and no generic
 * persisted-value helper to route through; `NavRail` reached the same conclusion
 * for `ccswitch.railExpanded` and kept its pair local rather than inventing a
 * shared abstraction on one caller's behalf. This module follows that decision,
 * and mirrors `lib/sidebarWidth.ts` clause for clause so the two read alike.
 * All access is wrapped: a blocked or unavailable localStorage degrades to the
 * default, never a crash.
 */
export const DRAWER_MIN = 320
export const DRAWER_MAX = 640
export const DRAWER_DEFAULT = 448
export const DRAWER_STORAGE_KEY = 'ccswitch.drawerWidth'

/** Clamp to [MIN, MAX] and round; non-finite input falls back to the default. */
export function clampDrawerWidth(px: number): number {
  if (!Number.isFinite(px)) return DRAWER_DEFAULT
  return Math.round(Math.min(DRAWER_MAX, Math.max(DRAWER_MIN, px)))
}

/** Read the persisted width, clamped; falls back to the default on miss/corrupt. */
export function loadDrawerWidth(): number {
  try {
    const raw = window.localStorage.getItem(DRAWER_STORAGE_KEY)
    if (raw === null) return DRAWER_DEFAULT
    return clampDrawerWidth(Number(raw))
  } catch {
    return DRAWER_DEFAULT
  }
}

/** Persist the width (clamped). Swallows storage errors — persistence is best-effort. */
export function saveDrawerWidth(px: number): void {
  try {
    window.localStorage.setItem(DRAWER_STORAGE_KEY, String(clampDrawerWidth(px)))
  } catch {
    /* ignore */
  }
}

/** Drawer width state, restored from and persisted to localStorage. The setter
 *  always clamps, so no caller can write a width outside the band. */
export function useDrawerWidth() {
  const [width, setWidthRaw] = useState(loadDrawerWidth)
  const setWidth = useCallback((px: number) => setWidthRaw(clampDrawerWidth(px)), [])
  useEffect(() => {
    saveDrawerWidth(width)
  }, [width])
  return [width, setWidth] as const
}
