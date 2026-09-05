import { useSyncExternalStore } from 'react'

export const NOTIFY_PREF_KEY = 'ccswitch.pipelineNotifications'
export const LEGACY_NOTIFY_PREF_KEY = 'prdash.notifications'
const listeners = new Set<() => void>()

export function readNotifyPref(): boolean {
  try {
    const v = localStorage.getItem(NOTIFY_PREF_KEY)
    if (v !== null) return v === 'true'
    const legacy = localStorage.getItem(LEGACY_NOTIFY_PREF_KEY)
    if (legacy !== null) {
      localStorage.setItem(NOTIFY_PREF_KEY, legacy)
      localStorage.removeItem(LEGACY_NOTIFY_PREF_KEY)
      return legacy === 'true'
    }
    return false
  } catch { return false }
}

export function writeNotifyPref(v: boolean): void {
  try { localStorage.setItem(NOTIFY_PREF_KEY, String(v)) } catch { /* storage unavailable */ }
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }

export function useNotifyPref(): [boolean, (v: boolean) => void] {
  const v = useSyncExternalStore(subscribe, readNotifyPref, () => false)
  return [v, writeNotifyPref]
}
