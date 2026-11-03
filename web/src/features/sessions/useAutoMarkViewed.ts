/**
 * useAutoMarkViewed — keeps the open session "read" for this viewer (D-10).
 *
 * - Opening a session posts `viewed` once.
 * - While it stays open on a visible tab, a new turn end (`updatedAt` changes)
 *   posts again, debounced so a burst of turn ends costs one request.
 * - Nothing is posted while the tab is hidden: a turn that ends then is unread
 *   until the viewer comes back, and coming back posts once.
 * - An optimistic placeholder (`isPendingSessionId`) has no server row; skipped.
 *
 * Mounted by `SessionBody` (the live open session) and by the archived Reader
 * page (`ArchivedReviewPage`), whose card shows the same unread bold.
 */
import { useEffect, useRef } from 'react'
import { useMarkSessionViewed } from '@/api/hooks'
import { usePageVisible } from '@/hooks/usePageVisible'
import { isPendingSessionId } from '@/lib/pendingSessions'

export const VIEWED_DEBOUNCE_MS = 2000

export function useAutoMarkViewed(sessionId: string, updatedAt: string | null | undefined): void {
  const visible = usePageVisible()
  const { mutate } = useMarkSessionViewed({ auto: true })
  // The session posted for the current visible stretch; cleared while hidden so
  // the tab coming back posts once.
  const openedRef = useRef<string | null>(null)
  // The `updatedAt` the last post covered; a change from it is a new turn end.
  const coveredRef = useRef<string | null | undefined>(undefined)
  const skip = isPendingSessionId(sessionId)

  useEffect(() => {
    if (skip || !visible) {
      openedRef.current = null
      return
    }
    // Open, switch session, or the tab coming back: post at once.
    if (openedRef.current !== sessionId) {
      openedRef.current = sessionId
      coveredRef.current = updatedAt
      mutate(sessionId)
      return
    }
    if (updatedAt === coveredRef.current) return
    const t = setTimeout(() => {
      coveredRef.current = updatedAt
      mutate(sessionId)
    }, VIEWED_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [sessionId, updatedAt, visible, skip, mutate])
}
