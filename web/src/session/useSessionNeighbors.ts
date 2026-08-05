// web/src/session/useSessionNeighbors.ts
import { useMemo } from 'react'
import { useSessions } from '@/api/hooks'
import { useSessionOrdering } from './useSessionOrdering'

export interface SessionNeighbors {
  /** Previous session id in the current list order; null when none. */
  prevId: string | null
  /** Next session id in the current list order; null when none. */
  nextId: string | null
  /** Position of the current session in the ordered list; -1 if absent. */
  index: number
  /** Length of the ordered list. */
  total: number
}

/**
 * Resolve the previous/next session ids relative to `sessionId`, walking the
 * SAME ordered list the dashboard renders (group/sort prefs included).
 * Navigation wraps around the ends. When the current session is hidden from the
 * visible list (its group is collapsed) the walk falls back to the full
 * ordering, so the chevrons keep working instead of disappearing. Returns null
 * neighbors when there is only one session, or when the current session is in
 * neither list (e.g. archived, or a freshly-created pending session) — the UI
 * hides the chevrons in that case.
 */
export function useSessionNeighbors(sessionId: string): SessionNeighbors {
  const { data } = useSessions()
  const sessions = useMemo(() => data ?? [], [data])
  const { orderedSessions, allOrderedSessions } = useSessionOrdering(sessions)

  return useMemo(() => {
    const inVisible = orderedSessions.some((s) => s.id === sessionId)
    const list = inVisible ? orderedSessions : allOrderedSessions
    const total = list.length
    const index = list.findIndex((s) => s.id === sessionId)
    if (index === -1 || total <= 1) {
      return { prevId: null, nextId: null, index, total }
    }
    const nextId = list[(index + 1) % total].id
    const prevId = list[(index - 1 + total) % total].id
    return { prevId, nextId, index, total }
  }, [orderedSessions, allOrderedSessions, sessionId])
}
