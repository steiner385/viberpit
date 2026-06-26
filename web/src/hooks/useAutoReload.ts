import { useEffect } from 'react'
import { useSessions } from '@/api/hooks'
import { useUpdatePending } from '@/hooks/useUpdatePending'
import { getSessionSocket } from '@/ws/client'
import { isSafeToReload, BUILD_SHA } from '@/lib/version'

// Per-tab marker: the bundle sha we last auto-reloaded FROM. If we reload and come back on the
// SAME bundle still flagged outdated — a deploy left the edge reporting a sha this bundle can
// never match, or a stale cache — reloading again is futile and loops forever (observed in prod:
// ~50 reloads/sec pegging the renderer). Guard so we auto-reload at most once per bundle; after
// that the NewVersionBanner offers a manual reload instead.
const RELOADED_FROM_KEY = 'ccswitch:auto-reloaded-from-sha'

function alreadyAutoReloadedFromThisBundle(): boolean {
  try {
    return sessionStorage.getItem(RELOADED_FROM_KEY) === BUILD_SHA
  } catch {
    return false // sessionStorage unavailable → fall through (no worse than before the guard)
  }
}

function markAutoReloaded(): void {
  try {
    sessionStorage.setItem(RELOADED_FROM_KEY, BUILD_SHA)
  } catch {
    /* private mode / disabled storage — best effort */
  }
}

/**
 * Silently reloads to the new bundle once it's safe: an update is available, the focused session
 * (the one the socket is attached to) is not mid-turn, and no prompts are client-buffered. Unsent
 * composer drafts survive a reload via localStorage, so they are not a gate. Re-evaluates whenever
 * the session list (status) or the buffered-prompt count changes.
 *
 * Auto-reloads at most ONCE per bundle: if the reload doesn't clear `updateAvailable` (the served
 * bundle still doesn't match the server sha), it stops looping and leaves the manual banner.
 */
export function useAutoReload(updateAvailable: boolean): void {
  const { data: sessions } = useSessions()
  const pending = useUpdatePending()

  useEffect(() => {
    if (!updateAvailable) return
    if (alreadyAutoReloadedFromThisBundle()) return
    let activeId: string | null = null
    try {
      activeId = getSessionSocket().activeSessionId
    } catch {
      activeId = null // jsdom: no socket → treat as no focused session
    }
    const focused = activeId ? sessions?.find((s) => s.id === activeId) : undefined
    if (isSafeToReload(focused?.status, pending.count)) {
      markAutoReloaded()
      location.reload()
    }
  }, [updateAvailable, sessions, pending.count])
}
