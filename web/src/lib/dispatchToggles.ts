/**
 * The dispatch bar's remembered Autopilot / Auto-archive ticks.
 *
 * Same shape as `lib/lastRepo.ts` and `lib/enginePreference.ts` — one tiny
 * module per persisted preference, every access wrapped so a blocked or
 * unavailable localStorage (private mode, quota, SSR) degrades to the default
 * (ticked = governed by the global settings) instead of crashing the dock.
 *
 * Only the literal strings 'true' / 'false' count as a stored choice; anything
 * else reads as the default, so a corrupt value can never silently un-govern
 * a new session.
 */
import { storageKey } from '@/lib/storageKey'

export type DispatchToggle = 'autopilot' | 'autoArchive'

const KEYS: Record<DispatchToggle, string> = {
  autopilot: storageKey('dispatch.autopilot'),
  autoArchive: storageKey('dispatch.autoArchive'),
}

export function getDispatchToggle(toggle: DispatchToggle): boolean {
  try {
    // No `migrateStorageNamespace`: these keys post-date the rebrand (#1368),
    // so there is no `ccswitch.` twin to carry forward.
    return window.localStorage.getItem(KEYS[toggle]) !== 'false'
  } catch {
    return true
  }
}

export function setDispatchToggle(toggle: DispatchToggle, on: boolean): void {
  try {
    window.localStorage.setItem(KEYS[toggle], String(on))
  } catch {
    /* ignore — persistence is best-effort */
  }
}
