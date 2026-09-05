import { useSyncExternalStore } from 'react'

export const RAIL_EXPANDED_KEY = 'ccswitch.railExpanded'
const listeners = new Set<() => void>()

export function readRailExpanded(): boolean {
  try { return localStorage.getItem(RAIL_EXPANDED_KEY) === 'true' } catch { return false }
}

export function writeRailExpanded(v: boolean): void {
  try { localStorage.setItem(RAIL_EXPANDED_KEY, String(v)) } catch { /* storage unavailable */ }
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

/** DEC-6: one store for the rail's expand/collapse, shared by NavRail and Settings › Appearance. */
export function useRailExpanded(): [boolean, (v: boolean) => void] {
  const v = useSyncExternalStore(subscribe, readRailExpanded, () => false)
  return [v, writeRailExpanded]
}
