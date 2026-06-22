import { useEffect } from 'react'
import { statusTier } from '@/session/statusTier'
import type { Session } from '@/api/types'

const BASE_TITLE = 'ccswitch'

/** Reflect the count of blocking ("needs you") sessions in the browser tab title,
 *  so a backgrounded tab surfaces work waiting on the user. */
export function useNeedsYouTitle(sessions: Session[]): void {
  const needsYou = sessions.filter((s) => statusTier(s.status) === 'needs-you').length
  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) ${BASE_TITLE} — needs you` : BASE_TITLE
    return () => { document.title = BASE_TITLE }
  }, [needsYou])
}
