import { useSyncExternalStore } from 'react'
import { storageKey, migrateStorageKey, migrateStorageNamespace } from '@/lib/storageKey'

export const NOTIFY_PREF_KEY = storageKey('pipelineNotifications')
export const LEGACY_NOTIFY_PREF_KEY = 'prdash.notifications'
const listeners = new Set<() => void>()

export function readNotifyPref(): boolean {
  try {
    // Two generations of legacy key, newest first: a value that survived both the
    // pre-namespace era AND the brand flip must land on the newer of the two.
    // `migrateStorageKey` only copies into an ABSENT target, so running the brand
    // migration first makes `prdash.` lose — and get cleared — as it should.
    migrateStorageNamespace(localStorage, NOTIFY_PREF_KEY)
    migrateStorageKey(localStorage, LEGACY_NOTIFY_PREF_KEY, NOTIFY_PREF_KEY)
    return localStorage.getItem(NOTIFY_PREF_KEY) === 'true'
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
