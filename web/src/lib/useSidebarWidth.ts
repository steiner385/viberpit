import { useSyncExternalStore } from 'react'
import { SIDEBAR_DEFAULT, loadSidebarWidth, saveSidebarWidth } from './sidebarWidth'

const listeners = new Set<() => void>()

/**
 * Persist a width and tell every subscriber. Always clamps (`saveSidebarWidth`
 * does), so no caller can write outside the band.
 */
export function writeSidebarWidth(px: number): void {
  saveSidebarWidth(px)
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

/**
 * Sidebar width as a [value, setter] pair; the setter clamps and persists.
 *
 * R15: this is ONE store shared by every subscriber (the same
 * `useSyncExternalStore`-over-a-listener-set shape as `shell/railExpanded.ts`),
 * not per-component `useState`. The list column (`shell/regions/ContextList`)
 * and Settings › Appearance both hold it, so with local state a Reset in
 * Settings wrote storage while the visible column kept its stale copy until a
 * reload, and a drag left the number in Settings stale the same way.
 *
 * The snapshot reads storage rather than caching, exactly as the rail-expanded
 * and notify-pref stores do: a value cached in the module would go stale
 * against a test (or another tab) that writes the key directly.
 */
export function useSidebarWidth() {
  const width = useSyncExternalStore(subscribe, loadSidebarWidth, () => SIDEBAR_DEFAULT)
  return [width, writeSidebarWidth] as const
}
