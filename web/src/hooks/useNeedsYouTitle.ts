import { useEffect } from 'react'
import { setNeedsYouCount } from '@/shell/documentTitle'
import { triageTier } from '@/session/triageTier'
import type { Session } from '@/api/types'

/** Reflect the count of blocking ("needs you") sessions in the browser tab title,
 *  so a backgrounded tab surfaces work waiting on the user. A session with a
 *  failing PR is counted even if its own status is calm — broken PRs need attention.
 *
 *  Does NOT write `document.title` itself — it reports the count into
 *  `shell/documentTitle.ts`, the single owner, which composes it with any
 *  base title `ui/PageHeader` has claimed. */
export function useNeedsYouTitle(sessions: Session[]): void {
  const needsYou = sessions.filter((s) => {
    const tier = triageTier(s)
    return tier === 'needs-you' || tier === 'error'
  }).length
  useEffect(() => {
    setNeedsYouCount(needsYou)
    return () => setNeedsYouCount(0)
  }, [needsYou])
}
