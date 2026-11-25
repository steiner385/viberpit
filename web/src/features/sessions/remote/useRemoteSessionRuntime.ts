/**
 * useRemoteSessionRuntime — the live view of a Remote Control session ViberPit does
 * not run, as the ordinary session runtime with its WRITE half swapped for REST.
 *
 * The read half is `useSessionRuntime` itself, attached to `remote:<id>` on the page's
 * own `/ws` socket: the server answers that attach with the same wire frames a ViberPit
 * session produces (`replayReset` … `replayComplete`, then live `textDelta`,
 * `toolCall`, `permissionRequest`, `question` …), so one reducer and one set of
 * transcript/card components render both. What the socket must NOT carry for a remote
 * id is a control frame — the server refuses every one with
 * `REMOTE_SESSION_FRAME_UNSUPPORTED` — so prompts, permission decisions and question
 * answers go to `/api/remote-sessions/{id}/prompt|answer` here instead. This is the one
 * adapter between the cards' normal `onDecision`/`onAnswer`/`onPrompt` callbacks and
 * the remote endpoints; the cards themselves are unchanged.
 *
 * Error handling (the R1 contract):
 * - `remote_stream_reset` (the server's watcher restarted) → re-attach.
 * - `remote_session_gone` / `REMOTE_SESSION_NOT_FOUND` → `gone`; the page disables input.
 * - `REMOTE_SESSION_NOT_WATCHED` on a write (the answer needs an open view) → re-attach,
 *   wait for the replay to finish, retry once.
 * - Anything else (incl. the person-only 403) → an inline `notice`, server's words.
 */
import { useCallback, useEffect, useState } from 'react'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { useSessionSocket } from '@/ws/client'
import type { SessionEvent } from '@/session/events'
import { apiErrorCode, apiErrorDetail } from '@/api/client'
import {
  remoteWsId, sendRemoteAnswer, sendRemotePrompt, type RemoteAnswerBody,
} from '@/api/remoteSessionsHooks'

/** How long a NOT_WATCHED retry waits for the re-attach's `replayComplete`. */
export const REATTACH_WAIT_MS = 5_000

const GONE_CODES = new Set(['remote_session_gone', 'REMOTE_SESSION_NOT_FOUND'])

/** Inline copy for a refused write; the server's own message wins where it sent one. */
function refusalNotice(err: unknown): string {
  const code = apiErrorCode(err)
  const message = apiErrorDetail(err).message
  if (code === 'SESSION_CONTROL_HUMAN_ONLY') {
    return message ?? 'Only a person can control this session — automation can watch it, not drive it.'
  }
  if (code === 'REMOTE_ASK_NOT_PENDING') {
    return message ?? 'That request is no longer pending — it may have been answered in the Claude app.'
  }
  return message ?? 'Could not reach the remote session. Try again.'
}

export function useRemoteSessionRuntime(remoteId: string) {
  const wsId = remoteWsId(remoteId)
  const base = useSessionRuntime(wsId)
  const { on, off } = useSessionSocket()
  const { reattach, dismissError, armedQuestionId, onChat } = base
  // Gone is learned two ways: a write refused NOT_FOUND (sticky here) or the socket's
  // own error frame (derived from the reducer's banner slot below).
  const [goneByWrite, setGone] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // A prompt the server refused goes back into the composer (its `returnedDraft`).
  const [returnedDraft, setReturnedDraft] = useState<{ text: string } | null>(null)

  // Socket-side errors arrive through the reducer's banner slot like any session's.
  const error = base.state.error
  const gone = goneByWrite || (error !== null && GONE_CODES.has(error.code))
  useEffect(() => {
    if (error?.code !== 'remote_stream_reset') return
    dismissError()
    reattach()
  }, [error, dismissError, reattach])

  /** Re-attach and resolve once its replay completes (or after REATTACH_WAIT_MS). */
  const reattachAndWait = useCallback(() => new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      off('sessionEvent', onEvent)
      resolve()
    }
    const onEvent = (e: SessionEvent) => {
      const sid = (e as { sessionId?: string }).sessionId
      if (e.type === 'replayComplete' && (!sid || sid === wsId)) finish()
    }
    const timer = setTimeout(finish, REATTACH_WAIT_MS)
    on('sessionEvent', onEvent)
    reattach()
  }), [on, off, reattach, wsId])

  /** Run a write; NOT_WATCHED re-attaches and retries once. Null = refused (noticed). */
  const write = useCallback(async <T,>(call: () => Promise<T>): Promise<T | null> => {
    setNotice(null)
    const refused = (err: unknown) => {
      if (GONE_CODES.has(apiErrorCode(err) ?? '')) setGone(true)
      else setNotice(refusalNotice(err))
      return null
    }
    try {
      return await call()
    } catch (err) {
      if (apiErrorCode(err) !== 'REMOTE_SESSION_NOT_WATCHED') return refused(err)
      await reattachAndWait()
      try {
        return await call()
      } catch (retryErr) {
        return refused(retryErr)
      }
    }
  }, [reattachAndWait])

  const answer = useCallback(
    (body: RemoteAnswerBody) => write(() => sendRemoteAnswer(remoteId, body)),
    [write, remoteId],
  )

  /** Permission cards. No standing grants on a remote session: 'always' is an allow. */
  const onDecision = useCallback(
    (permId: string, decision: 'allow' | 'deny' | 'always', message?: string) => {
      void answer({
        askId: permId,
        decision: decision === 'deny' ? 'deny' : 'allow',
        ...(message !== undefined && decision === 'deny' ? { message } : {}),
      })
    },
    [answer],
  )

  /** Question cards (`questionId` is `<request_id>:<index>`). */
  const onAnswer = useCallback(
    (questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => {
      void answer({ askId: questionId, selected, mode })
    },
    [answer],
  )

  /** The composer. A question armed for chat takes the text as its answer instead. */
  const onPrompt = useCallback(
    (text: string) => {
      if (gone) return
      if (armedQuestionId) {
        const questionId = armedQuestionId
        void answer({ askId: questionId, selected: [text], mode: 'chat' })
          .then((res) => { if (res) onChat(questionId) })
        return
      }
      setReturnedDraft(null)
      void write(() => sendRemotePrompt(remoteId, text))
        .then((res) => { if (!res) setReturnedDraft({ text }) })
    },
    [gone, armedQuestionId, answer, onChat, write, remoteId],
  )

  return {
    /** The read half: transcript state, connection, chat-arming. */
    base,
    gone,
    notice,
    dismissNotice: () => setNotice(null),
    returnedDraft,
    onPrompt,
    onDecision,
    onAnswer,
  }
}
