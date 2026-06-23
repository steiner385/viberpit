import { useEffect } from 'react'
import { useSessions } from '@/api/hooks'
import { useUpdatePending } from '@/hooks/useUpdatePending'
import { getSessionSocket } from '@/ws/client'
import { isSafeToReload } from '@/lib/version'

/**
 * Silently reloads to the new bundle once it's safe: an update is available, the focused session
 * (the one the socket is attached to) is not mid-turn, and no prompts are client-buffered. Unsent
 * composer drafts survive a reload via localStorage, so they are not a gate. Re-evaluates whenever
 * the session list (status) or the buffered-prompt count changes.
 */
export function useAutoReload(updateAvailable: boolean): void {
  const { data: sessions } = useSessions()
  const pending = useUpdatePending()

  useEffect(() => {
    if (!updateAvailable) return
    let activeId: string | null = null
    try {
      activeId = getSessionSocket().activeSessionId
    } catch {
      activeId = null // jsdom: no socket → treat as no focused session
    }
    const focused = activeId ? sessions?.find((s) => s.id === activeId) : undefined
    if (isSafeToReload(focused?.status, pending.count)) {
      location.reload()
    }
  }, [updateAvailable, sessions, pending.count])
}
