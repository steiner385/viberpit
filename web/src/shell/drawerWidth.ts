import { useSyncExternalStore } from 'react'

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
 * persisted-value helper to route through, so this module keeps its own pair
 * and mirrors `lib/sidebarWidth.ts` clause for clause, the two reading alike.
 * All access is wrapped: a blocked or unavailable localStorage degrades to the
 * default, never a crash.
 *
 * `useDrawerWidth` below is the shared-store shape `shell/railExpanded.ts` uses
 * (R15) — the value has two holders, so it cannot be per-component state.
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

const listeners = new Set<() => void>()

/** Persist a width and tell every subscriber. Always clamps (`saveDrawerWidth` does). */
export function writeDrawerWidth(px: number): void {
  saveDrawerWidth(px)
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

/**
 * Drawer width as a [value, setter] pair; the setter clamps and persists.
 *
 * R15: ONE store shared by every subscriber (`useSyncExternalStore` over a
 * listener set, as in `railExpanded.ts`), not per-component `useState` — the
 * drawer (`shell/regions/DrawerHost`) and Settings › Appearance both hold it,
 * and with local copies a Reset in Settings left the open drawer at its old
 * width until a reload. The snapshot reads storage rather than caching, for the
 * same reason as `lib/useSidebarWidth.ts`.
 */
export function useDrawerWidth() {
  const width = useSyncExternalStore(subscribe, loadDrawerWidth, () => DRAWER_DEFAULT)
  return [width, writeDrawerWidth] as const
}
