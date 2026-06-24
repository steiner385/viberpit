import { useEffect } from 'react'
import { triageTier } from '@/session/triageTier'
import type { Session } from '@/api/types'

const BASE_TITLE = 'ccswitch'

/** Reflect the count of blocking ("needs you") sessions in the browser tab title,
 *  so a backgrounded tab surfaces work waiting on the user. A session with a
 *  failing PR is counted even if its own status is calm — broken PRs need attention. */
export function useNeedsYouTitle(sessions: Session[]): void {
  const needsYou = sessions.filter((s) => {
    const tier = triageTier(s)
    return tier === 'needs-you' || tier === 'error'
  }).length
  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) ${BASE_TITLE} — needs you` : BASE_TITLE
    return () => { document.title = BASE_TITLE }
  }, [needsYou])
}
