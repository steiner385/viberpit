import { useCallback, useMemo, useRef } from 'react'

export interface DraftAttachment { id: string; mime: string; name: string }
export interface SessionDraft { text: string; attachments: DraftAttachment[] }

const EMPTY: SessionDraft = { text: '', attachments: [] }
const keyFor = (sessionId: string) => `ccswitch:draft:${sessionId}`

function read(sessionId?: string): SessionDraft {
  if (!sessionId) return { ...EMPTY }
  try {
    const raw = localStorage.getItem(keyFor(sessionId))
    if (!raw) return { ...EMPTY }
    const parsed = JSON.parse(raw)
    return {
      text: typeof parsed.text === 'string' ? parsed.text : '',
      attachments: Array.isArray(parsed.attachments) ? parsed.attachments : [],
    }
  } catch {
    return { ...EMPTY }
  }
}

/**
 * Per-session unsent-draft store backed by localStorage. `initial` is read once
 * on mount (stable for the component's lifetime); call `save` on change and
 * `clear` on submit. A no-op when `sessionId` is undefined.
 */
export function useSessionDraft(sessionId?: string) {
  const initialRef = useRef<SessionDraft>(read(sessionId))

  const clear = useCallback(() => {
    if (!sessionId) return
    try { localStorage.removeItem(keyFor(sessionId)) } catch { /* ignore */ }
  }, [sessionId])

  const save = useCallback((draft: SessionDraft) => {
    if (!sessionId) return
    const empty = draft.text.trim() === '' && draft.attachments.length === 0
    try {
      if (empty) localStorage.removeItem(keyFor(sessionId))
      else localStorage.setItem(keyFor(sessionId), JSON.stringify(draft))
    } catch { /* ignore quota/availability errors */ }
  }, [sessionId])

  return useMemo(() => ({ initial: initialRef.current, save, clear }), [save, clear])
}
