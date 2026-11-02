import { useCallback, useState } from 'react'
import { storageKey } from '@/lib/storageKey'
import type { LineComment } from './reviewComments'

const draftKey = (sessionId: string) => storageKey(`reviewDraft:${sessionId}`)

function load(sessionId: string): LineComment[] {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(draftKey(sessionId)) ?? '[]')
    return Array.isArray(parsed) ? (parsed as LineComment[]) : []
  } catch {
    return []
  }
}

function save(sessionId: string, comments: LineComment[]): void {
  try {
    if (comments.length === 0) window.sessionStorage.removeItem(draftKey(sessionId))
    else window.sessionStorage.setItem(draftKey(sessionId), JSON.stringify(comments))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

/** The unsent review comments for one session, kept in sessionStorage. */
export function useReviewDraft(sessionId: string) {
  const [state, setState] = useState(() => ({ sessionId, comments: load(sessionId) }))
  // Switching sessions re-reads that session's draft during render (no stale flash).
  let current = state
  if (state.sessionId !== sessionId) {
    current = { sessionId, comments: load(sessionId) }
    setState(current)
  }

  const commit = useCallback(
    (next: (prev: LineComment[]) => LineComment[]) => {
      setState((s) => {
        const comments = next(s.sessionId === sessionId ? s.comments : load(sessionId))
        save(sessionId, comments)
        return { sessionId, comments }
      })
    },
    [sessionId],
  )

  const add = useCallback((c: LineComment) => commit((p) => [...p, c]), [commit])
  const update = useCallback(
    (id: string, body: string) => commit((p) => p.map((c) => (c.id === id ? { ...c, body } : c))),
    [commit],
  )
  const remove = useCallback((id: string) => commit((p) => p.filter((c) => c.id !== id)), [commit])
  const clear = useCallback(() => commit(() => []), [commit])

  return { comments: current.comments, add, update, remove, clear }
}
