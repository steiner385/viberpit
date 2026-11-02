import { useCallback, useEffect, useState } from 'react'
import { storageKey } from '@/lib/storageKey'
import type { LineComment } from './reviewComments'

const draftKey = (sessionId: string) => storageKey(`reviewDraft:${sessionId}`)

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null

/** Storage is user-writable and outlives code versions: keep only well-formed entries. */
function isLineComment(v: unknown): v is LineComment {
  if (!isRecord(v) || !isRecord(v.anchor)) return false
  const { path, side, start, end } = v.anchor
  return typeof v.id === 'string' && typeof v.body === 'string'
    && Array.isArray(v.quote) && v.quote.every((l) => typeof l === 'string')
    && (path === null || typeof path === 'string') && (side === 'new' || side === 'old')
    && Number.isInteger(start) && Number.isInteger(end)
}

function load(sessionId: string): LineComment[] {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(draftKey(sessionId)) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter(isLineComment) : []
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

  // Persist after commit, keyed on the state's own session — never from inside an updater.
  useEffect(() => save(state.sessionId, state.comments), [state.sessionId, state.comments])

  const commit = useCallback(
    (next: (prev: LineComment[]) => LineComment[]) => {
      setState((s) => ({ sessionId, comments: next(s.sessionId === sessionId ? s.comments : load(sessionId)) }))
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
