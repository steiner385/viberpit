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
import { apiErrorCode, ApiRequestError } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { useComposerDraft } from '@/shell/sessionGestures'
import type { Turn } from '@/session/reducer'
import { rewindTargetFor } from '@/session/rewindTarget'

export const loadRewindFlow = () => import('./rewindFlow')
const RewindConfirmDialog = lazy(() => loadRewindFlow().then((m) => ({ default: m.RewindConfirmDialog })))

/** One error toast for a failed rewind or undo, with `rewindCopy`'s line for its code
 *  (an undo's `REWIND_FAILED` lists the paths it could not restore). Returns the code. */
export function failRewind(err: unknown, undo = false): string | null {
  const code = apiErrorCode(err)
  const message = extractMutationErrorMessage(err, '')
  const detail = err instanceof ApiRequestError ? err.body.detail as { failed?: unknown } | undefined : undefined
  const failed = Array.isArray(detail?.failed) ? detail.failed.map(String) : []
  void loadRewindFlow().then((m) => m.toastRewindError(code, message, undo, failed))
  return code
}

/** What the confirm dialog runs: one rewind, settled through `done` exactly once. */
export interface RewindRequest {
  toTurnId: string
  edit: boolean
  /** Rewind & retry: the confirm says the prompt is sent again, which ends the undo. */
  retry: boolean
  preview: (toTurnId: string) => Promise<RewindPreview>
  rewind: (toTurnId: string, mode: RewindMode) => Promise<RewindResult>
  /** Put the cut prompt back in this session's composer (Edit & resend). */
  setDraft: (text: string) => void
  /** Edit & resend over a non-empty draft: the confirm says it will be replaced. */
  replacesDraft: boolean
  /** Toast a refusal (`failRewind`). */
  fail: (err: unknown) => void
  done: (result: RewindResult | null) => void
}

export interface Rewind {
  /** Confirm and rewind to user turn `turn`; null when it has no target or one is open. */
  start: (turn: Turn, opts: { edit: boolean; retry?: boolean }) => Promise<RewindResult | null>
  /** A confirm dialog is open (one at a time: `start` refuses a second). */
  active: boolean
  /** Undo rewind `rewindId`; a refusal toasts, skipped paths are listed. */
  undo: (rewindId: string) => Promise<void>
  /** The confirm dialog while a rewind is open — render it where `useRewind` is called. */
  element: ReactNode
}

/** `onExpired(rewindId)`: the server refused an undo as expired — drop its divider. */
export function useRewind(sessionId: string, { onExpired }: { onExpired?: (rewindId: string) => void } = {}): Rewind {
  const [request, setRequest] = useState<RewindRequest | null>(null)
  const open = useRef(false)
  const { preview, rewind } = useRewindSession(sessionId)
  const { undo: postUndo } = useUndoRewind(sessionId)
  const draft = useComposerDraft()

  const start = useCallback((turn: Turn, { edit, retry = false }: { edit: boolean; retry?: boolean }) => {
    const toTurnId = rewindTargetFor(turn)
    if (toTurnId === null || open.current) return Promise.resolve(null)
    open.current = true
    return new Promise<RewindResult | null>((resolve) => setRequest({
      toTurnId, edit, retry, preview, rewind, fail: failRewind,
      setDraft: (text) => draft.set(sessionId, text),
      replacesDraft: edit && draft.has(sessionId),
      done: (result) => { open.current = false; setRequest(null); resolve(result) },
    }))
  }, [preview, rewind, draft, sessionId])

  const undo = useCallback(async (rewindId: string) => {
    try {
      const { skipped } = await postUndo(rewindId)
      if (skipped.length) (await loadRewindFlow()).toastUndoSkipped(skipped)
    } catch (err) {
      if (failRewind(err, true) === 'REWIND_UNDO_EXPIRED') onExpired?.(rewindId)
    }
  }, [postUndo, onExpired])

  const element = request && createElement(Suspense, { fallback: null },
    createElement(RewindConfirmDialog, { request }))
  return { start, undo, element, active: request !== null }
}
