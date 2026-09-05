/**
 * `useArchiveAdvance` — where `/sessions/:id` goes when `:id` stops being live.
 *
 * ## This is ported behaviour, not new behaviour
 *
 * It is `App.tsx`'s `SessionRoute` effects (L93–158 before Task 17), moved out
 * of the deleted routed-shell fork and onto the Sessions page, which is now the
 * only host of an open session. Three tests in `App.test.tsx` pin it — archive
 * advances to the neighbour, advances even when the open session's group is
 * collapsed, and Back does NOT advance — and they define the contract.
 *
 * Two distinct paths lead here, and both existed before:
 *
 *   • `onDone` — the header's Archive/Delete succeeded. `useCloseSession`'s
 *     per-call `onSuccess` fires, the host advances immediately, and the
 *     navigation is PUSHED: the user asked for it, so Back returning to the
 *     (now archived) session is coherent.
 *
 *   • the effect — the archive API answered 503/409. `useCloseSession` fires
 *     `onError`, so `onDone` is never called; `onSettled` then confirms the
 *     session really is gone and the detail view is stranded on a session that
 *     has left the live list (its title degrades to a raw id slice). The effect
 *     is the safety net, and it REPLACES: the session that is gone is not a
 *     place Back should return to.
 *
 * The effect also covers the other way a session can be archived while open —
 * a stale bookmark. If the id was NEVER in the live list (an automation log
 * link to an already-archived session), there is no neighbour to advance to and
 * the right destination is the read-only transcript, `/sessions/archived/:id`.
 *
 * ## Why the ordering here is unfiltered
 *
 * `AppShell`'s own `advanceFrom` (list-initiated close/delete) reads the
 * SEARCH-FILTERED ordering, because there the user is looking at that filtered
 * list. This hook reads the plain ordering, exactly as `App.tsx` did, because
 * the search box is not reachable from inside an open session — filtering it by
 * a query the user cannot see would be a second, invisible opinion. Both route
 * the actual rule through `nextSessionAfterClose`, which stays the one owner of
 * "which neighbour".
 */
import { useCallback, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useSessions, useArchivedSessions } from '@/api/hooks'
import { useSessionOrdering } from '@/session/useSessionOrdering'
import { nextSessionAfterClose } from '@/session/nextAfterArchive'
import { SESSIONS_PATH, archivedSessionPath, sessionPath } from '@/session/sessionNavState'

/**
 * @param sessionId the open session, or `undefined` on `/sessions` with none open.
 * @returns the `onDone` handler for the session's Archive/Delete actions.
 */
export function useArchiveAdvance(sessionId: string | undefined): () => void {
  const navigate = useNavigate()
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions()
  const { data: archived = [] } = useArchivedSessions()
  const { orderedSessions, allOrderedSessions } = useSessionOrdering(sessions)

  // Snapshot of the ordered ids from the last render where the session was live.
  // The effect below reads it to find the correct neighbour even after
  // `orderedSessions` has already dropped the session. A ref rather than state:
  // reading refs in effects is fine, and state would schedule an extra render on
  // every session-list change.
  const lastOrderRef = useRef({
    visible: orderedSessions.map((s) => s.id),
    all: allOrderedSessions.map((s) => s.id),
  })
  useEffect(() => {
    if (sessionsLoading || !sessionId) return
    if (sessions.some((s) => s.id === sessionId)) {
      lastOrderRef.current = {
        visible: orderedSessions.map((s) => s.id),
        all: allOrderedSessions.map((s) => s.id),
      }
    }
  }, [sessions, sessionsLoading, sessionId, orderedSessions, allOrderedSessions])

  useEffect(() => {
    if (!sessionId || !archived.some((s) => s.id === sessionId)) return
    const wasLive =
      lastOrderRef.current.visible.includes(sessionId) ||
      lastOrderRef.current.all.includes(sessionId)
    if (!wasLive) {
      navigate(archivedSessionPath(sessionId), { replace: true })
      return
    }
    const next = nextSessionAfterClose(
      lastOrderRef.current.visible,
      lastOrderRef.current.all,
      sessionId,
    )
    navigate(next ? sessionPath(next) : SESSIONS_PATH, { replace: true })
  }, [archived, sessionId, navigate])

  return useCallback(() => {
    if (!sessionId) return
    // Fall back to the full ordering: the open session may be hidden from the
    // visible list (its group collapsed) and must still advance to a neighbour.
    const next = nextSessionAfterClose(
      orderedSessions.map((s) => s.id),
      allOrderedSessions.map((s) => s.id),
      sessionId,
    )
    navigate(next ? sessionPath(next) : SESSIONS_PATH)
  }, [sessionId, orderedSessions, allOrderedSessions, navigate])
}
