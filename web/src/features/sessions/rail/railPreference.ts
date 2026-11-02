/**
 * Whether the session rail is collapsed (D-6), per browser. Same shape as
 * `diff/diffViewPreference.ts`: every access is wrapped so a blocked
 * localStorage degrades to the default (expanded). The key is new with the
 * rail, so there is no pre-rebrand value to migrate.
 */
import { useCallback, useState } from 'react'
import { storageKey } from '@/lib/storageKey'

export const RAIL_COLLAPSED_KEY = storageKey('sessionRail.collapsed')

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(RAIL_COLLAPSED_KEY) === 'true'
  } catch {
    return false
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(RAIL_COLLAPSED_KEY, String(collapsed))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

/** The remembered collapsed state and a toggle that persists it. */
export function useRailCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const toggle = useCallback(() => {
    writeCollapsed(!collapsed)
    setCollapsed(!collapsed)
  }, [collapsed])
  return [collapsed, toggle]
}
