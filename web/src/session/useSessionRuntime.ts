/**
 * useSessionRuntime — the live runtime for a single session.
 *
 * Lifts the socket wiring, transcript reducer, derived status-strip data, and
 * action handlers out of the SessionView component so they can be shared between
 * the mobile full-screen SessionView and the desktop master-detail layout (where
 * the transcript body and the message composer live in separate parts of the tree).
 *
 * Behavior mirrors what SessionView previously did inline.
 */

import { useEffect, useReducer, useState, useCallback, useRef } from 'react'
import { useSessionSocket } from '@/ws/client'
import type { ConnectionState, WsRotatedMsg, WsErrorMsg } from '@/ws/client'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { TranscriptState } from '@/session/reducer'
import type { SessionEvent } from '@/session/events'
import { selectSubAgents } from '@/session/selectors'
import { remainingContextPct } from '@/lib/contextWindow'
import { useSessions, useAccounts } from '@/api/hooks'
import type { ToolDetailMode } from '@/session/groupBlocks'

export type ImageRef = { sha: string; mime: string; name: string }

export interface SessionRuntime {
  sessionId: string
  state: TranscriptState
  mode: string
  toolDetailMode: ToolDetailMode
  toggleToolDetail: () => void
  connectionState: ConnectionState
  title: string
  account: string
  quota: { sessionPct: number; weeklyPct: number } | null
  contextPct: number | null
  subAgents: ReturnType<typeof selectSubAgents>
  hasOpenAction: boolean
  /** True when at least one open permission block is pending (permission blocks only). */
  hasOpenPermission: boolean
  /** Question currently armed for a free-flowing chat reply (next prompt answers it), or null. */
  armedQuestionId: string | null
  /** True when a question is armed for chat — the composer shows a "Reply" affordance. */
  chatArmed: boolean
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  /** Set/clear the chat-armed question (toggles off if the same id is passed again). */
  onChat: (questionId: string) => void
  onPrompt: (text: string, images: ImageRef[]) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  onCancelQueued: (index: number) => void
}

export function useSessionRuntime(sessionId: string, initialPrompt?: string): SessionRuntime {
  const {
    attach,
    detach,
    on,
    off,
    connectionState,
    prompt,
    sendInterrupt,
    permissionResponse,
    answerQuestion,
    setPermissionMode,
    cancelQueued,
  } = useSessionSocket()

  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)
  const [mode, setMode] = useState('interactive')
  const [armedQuestionId, setArmedQuestionId] = useState<string | null>(null)
  const modeRef = useRef('interactive')
  const [toolDetailMode, setToolDetailMode] = useState<ToolDetailMode>('auto')
  const toggleToolDetail = useCallback(
    () => setToolDetailMode((m) =>
      m === 'auto' ? 'all-closed' : m === 'all-closed' ? 'all-open' : 'auto',
    ),
    [],
  )

  // Attach / detach + subscribe to session events
  useEffect(() => {
    const onEvent = (e: SessionEvent) => dispatch(e)
    const onRotated = (msg: WsRotatedMsg) => {
      if (msg.sessionId !== sessionId) return
      // Route into the reducer so the rotation is anchored into the transcript
      // timeline (the single source of ordering) and scrolls with history.
      dispatch({ type: '_rotation', from: msg.fromAccount, to: msg.toAccount, reason: msg.reason })
    }
    // I3 — surface direct `error` frames (SESSION_NOT_FOUND, frame_too_large, etc.)
    const onError = (msg: WsErrorMsg) => {
      dispatch({ type: 'error', code: msg.code, message: msg.message, resetsAt: msg.resetsAt ?? null })
    }

    on('sessionEvent', onEvent)
    on('rotated', onRotated)
    on('error', onError)
    // Seed the user's prompt optimistically so it shows the instant the session
    // opens, before the SDK echoes it back. The reducer adopts (re-keys) this
    // turn when the real `user` event arrives, so there's no duplicate. The seed
    // is idempotent, so React StrictMode's double-effect is harmless. One-shot at
    // mount: `initialPrompt` is intentionally excluded from the dep array below.
    if (initialPrompt) dispatch({ type: '_seedUserPrompt', text: initialPrompt })
    attach(sessionId)

    return () => {
      off('sessionEvent', onEvent)
      off('rotated', onRotated)
      off('error', onError)
      detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // Decision handler for permission cards
  const onDecision = useCallback(
    (permId: string, decision: 'allow' | 'deny' | 'always') => {
      permissionResponse(sessionId, permId, decision)
      dispatch({ type: '_resolvePermission', permId })
    },
    [sessionId, permissionResponse],
  )

  // Answer handler for question cards. The answer is delivered as a tool result,
  // NOT a user prompt, so the server never echoes it back — optimistically echo the
  // chosen labels into the transcript as a user bubble.
  const onAnswer = useCallback(
    (questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => {
      const sent = answerQuestion(sessionId, questionId, selected, mode)
      if (!sent) {
        // The socket wasn't open — the answer never left the browser. Keep the
        // question interactive so it can be retried once reconnected, and surface
        // a notice instead of silently swallowing the click.
        dispatch({
          type: 'error',
          code: 'answer_not_sent',
          message: "Not connected — your answer wasn't sent. It will stay here; try again once you're reconnected.",
        })
        return
      }
      dispatch({ type: '_resolveQuestion', questionId })
      if (selected.length > 0) {
        dispatch({ type: 'user', turnId: `answer:${questionId}`, text: selected.join(', ') })
      }
      // If this question was armed for chat, the answer resolves it — disarm.
      setArmedQuestionId((cur) => (cur === questionId ? null : cur))
    },
    [sessionId, answerQuestion],
  )

  // Toggle "armed for chat" for a question: the next composer send becomes its answer.
  const onChat = useCallback(
    (questionId: string) => {
      setArmedQuestionId((cur) => (cur === questionId ? null : questionId))
    },
    [],
  )

  // Composer handlers — when a question is armed for chat, the next message answers
  // that question (mode='chat') and unblocks the turn; otherwise it's a normal prompt.
  // (Chat replies go through the answer channel, which carries no images.)
  const onPrompt = useCallback(
    (text: string, images: ImageRef[]) => {
      if (armedQuestionId) {
        answerQuestion(sessionId, armedQuestionId, [text], 'chat')
        dispatch({ type: '_resolveQuestion', questionId: armedQuestionId })
        dispatch({ type: 'user', turnId: `answer:${armedQuestionId}`, text })
        setArmedQuestionId(null)
        return
      }
      prompt(sessionId, text, images)
    },
    [sessionId, prompt, answerQuestion, armedQuestionId],
  )
  const onInterrupt = useCallback(
    () => {
      setArmedQuestionId(null)
      sendInterrupt(sessionId)
    },
    [sessionId, sendInterrupt],
  )
  const onSetMode = useCallback(
    (m: string) => {
      setMode(m)
      setPermissionMode(sessionId, m)
    },
    [sessionId, setPermissionMode],
  )
  const onCancelQueued = useCallback(
    (index: number) => cancelQueued(sessionId, index),
    [sessionId, cancelQueued],
  )

  // Keep modeRef current so external keydown handlers can read the latest mode.
  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  // Determine if any open permission/question block exists.
  const hasOpenAction = state.turns.some((t) =>
    t.blocks.some((b) => b.kind === 'permission' || b.kind === 'question'),
  )
  const hasOpenPermission = state.turns.some((t) => t.blocks.some((b) => b.kind === 'permission'))

  // If the armed question is no longer present (resolved/replaced), disarm.
  useEffect(() => {
    if (!armedQuestionId) return
    const stillOpen = state.turns.some((t) =>
      t.blocks.some((b) => b.kind === 'question' && b.questionId === armedQuestionId),
    )
    // eslint-disable-next-line react-hooks/set-state-in-effect -- disarm in sync with transcript changes
    if (!stillOpen) setArmedQuestionId(null)
  }, [state.turns, armedQuestionId])

  // ── Status-strip derived data ──────────────────────────────────────────────
  // Account: the session record's account, overridden by the latest rotation.
  const { data: sessions } = useSessions()
  const { data: accounts } = useAccounts()
  const sessionRecord = sessions?.find((s) => s.id === sessionId)

  // Header title: prefer the session's name (the AI-generated/auto title, the
  // same value shown on the dashboard card) so the two stay consistent. Fall
  // back to a live transcript `title` event, then the sessionId prefix.
  const title = sessionRecord?.name ?? state.title ?? sessionId.slice(0, 8)
  const latestRotation =
    state.rotations.length > 0 ? state.rotations[state.rotations.length - 1].to : null
  const account = latestRotation ?? sessionRecord?.accountName ?? '—'

  const accountUsage = accounts?.find((a) => a.name === account)?.usage ?? null
  const quota = accountUsage
    ? { sessionPct: accountUsage.session.pct, weeklyPct: accountUsage.weekly.pct }
    : null

  const contextPct = remainingContextPct(state.usage, state.model)
  const subAgents = selectSubAgents(state.turns)

  return {
    sessionId,
    state,
    mode,
    toolDetailMode,
    toggleToolDetail,
    connectionState,
    title,
    account,
    quota,
    contextPct,
    subAgents,
    hasOpenAction,
    hasOpenPermission,
    armedQuestionId,
    chatArmed: armedQuestionId !== null,
    onDecision,
    onAnswer,
    onChat,
    onPrompt,
    onInterrupt,
    onSetMode,
    onCancelQueued,
  }
}
