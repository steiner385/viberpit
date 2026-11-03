/**
 * "Rewind to here" (D-9), shared by the message menu, `/rewind` and Rewind & retry:
 * `start(turn, {edit})` opens the confirm dialog (built from the dry run), rewinds on
 * confirm and resolves with the result — or null when cancelled, refused or failed
 * (one error toast with the mapped copy). With `edit`, the cut prompt goes back in
 * this session's composer. The dialog, the toasts and the copy load on first use
 * (`rewindFlow`), off the entry chunk (bundle budget).
 */
import { createElement, lazy, Suspense, useCallback, useRef, useState, type ReactNode } from 'react'
import { useRewindSession, useUndoRewind, type RewindPreview, type RewindResult, type RewindMode } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { useComposerSetDraft } from '@/shell/sessionGestures'
import type { Turn } from '@/session/reducer'
import { rewindTargetFor } from '@/session/rewindTarget'

export const loadRewindFlow = () => import('./rewindFlow')
const RewindConfirmDialog = lazy(() => loadRewindFlow().then((m) => ({ default: m.RewindConfirmDialog })))

/** One error toast for a failed rewind or undo, with `rewindCopy`'s line for its code. */
export function failRewind(err: unknown, undo = false): void {
  const code = apiErrorCode(err)
  const message = extractMutationErrorMessage(err, '')
  void loadRewindFlow().then((m) => m.toastRewindError(code, message, undo))
}

/** What the confirm dialog runs: one rewind, settled through `done` exactly once. */
export interface RewindRequest {
  toTurnId: string
  edit: boolean
  preview: (toTurnId: string) => Promise<RewindPreview>
  rewind: (toTurnId: string, mode: RewindMode) => Promise<RewindResult>
  /** Put the cut prompt back in this session's composer (Edit & resend). */
  setDraft: (text: string) => void
  /** Toast a refusal (`failRewind`). */
  fail: (err: unknown) => void
  done: (result: RewindResult | null) => void
}

export interface Rewind {
  /** Confirm and rewind to user turn `turn`; null when it has no target or one is open. */
  start: (turn: Turn, opts: { edit: boolean }) => Promise<RewindResult | null>
  /** Undo rewind `rewindId`; a refusal toasts, skipped paths are listed. */
  undo: (rewindId: string) => Promise<void>
  /** The confirm dialog while a rewind is open — render it where `useRewind` is called. */
  element: ReactNode
}

export function useRewind(sessionId: string): Rewind {
  const [request, setRequest] = useState<RewindRequest | null>(null)
  const open = useRef(false)
  const { preview, rewind } = useRewindSession(sessionId)
  const { undo: postUndo } = useUndoRewind(sessionId)
  const setDraft = useComposerSetDraft()

  const start = useCallback((turn: Turn, { edit }: { edit: boolean }) => {
    const toTurnId = rewindTargetFor(turn)
    if (toTurnId === null || open.current) return Promise.resolve(null)
    open.current = true
    return new Promise<RewindResult | null>((resolve) => setRequest({
      toTurnId, edit, preview, rewind, fail: failRewind,
      setDraft: (text) => setDraft(sessionId, text),
      done: (result) => { open.current = false; setRequest(null); resolve(result) },
    }))
  }, [preview, rewind, setDraft, sessionId])

  const undo = useCallback(async (rewindId: string) => {
    try {
      const { skipped } = await postUndo(rewindId)
      if (skipped.length) (await loadRewindFlow()).toastUndoSkipped(skipped)
    } catch (err) {
      failRewind(err, true)
    }
  }, [postUndo])

  const element = request && createElement(Suspense, { fallback: null },
    createElement(RewindConfirmDialog, { request }))
  return { start, undo, element }
}
