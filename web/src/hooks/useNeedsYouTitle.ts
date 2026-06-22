import { useEffect } from 'react'
import { useSessions } from '@/api/hooks'
import { statusTier } from '@/session/statusTier'

const BASE_TITLE = 'ccswitch'

/**
 * Drive the browser tab title from the live session list.
 *
 * When N>0 sessions are in the `needs-you` tier (blocked on the user — a
 * permission, a question, or a plan awaiting approval) the title becomes
 * `(N) ccswitch — needs you`, so the user notices across tabs even when this
 * one is in the background. Otherwise it resets to `ccswitch`.
 *
 * Sourced from useSessions() (the 5s REST poll), which carries the rich
 * persisted status for every session — including those the user is not
 * currently viewing.
 */
export function useNeedsYouTitle(): void {
  const { data: sessions } = useSessions()

  useEffect(() => {
    const needsYou = (sessions ?? []).filter((s) => statusTier(s.status) === 'needs-you').length
    document.title = needsYou > 0 ? `(${needsYou}) ${BASE_TITLE} — needs you` : BASE_TITLE
    return () => {
      // Restore the plain title when the consumer unmounts.
      document.title = BASE_TITLE
    }
  }, [sessions])
}
