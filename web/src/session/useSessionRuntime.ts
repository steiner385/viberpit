/**
 * useSessionRuntime — the live runtime for a single session.
 *
 * Lifts the socket wiring, transcript reducer, derived status-strip data, and
 * action handlers out of the SessionView component so they can be shared between
 * the mobile full-screen SessionView and the desktop master-detail layout (where
 * the transcript body and the message composer live in separate parts of the tree).
 *
 * Behavior is identical to what SessionView previously did inline.
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

export interface Rotation {
  from: string
  to: string
  reason?: string
}

export interface SessionRuntime {
  sessionId: string
  state: TranscriptState
  mode: string
  rotations: Rotation[]
  connectionState: ConnectionState
  title: string
  account: string
  quota: { sessionPct: number; weeklyPct: number } | null
  contextPct: number | null
  subAgents: ReturnType<typeof selectSubAgents>
  hasOpenAction: boolean
  ariaAnnouncement: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
  onPrompt: (text: string) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  onCancelQueued: (index: number) => void
}

export function useSessionRuntime(sessionId: string): SessionRuntime {
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
  const modeRef = useRef('interactive')
  const [rotations, setRotations] = useState<Rotation[]>([])

  // Track whether replay just finished so we can announce "History loaded" once.
  const prevReplayingRef = useRef(true)
  const [ariaAnnouncement, setAriaAnnouncement] = useState('')

  // Attach / detach + subscribe to session events
  useEffect(() => {
    const onEvent = (e: SessionEvent) => dispatch(e)
    const onRotated = (msg: WsRotatedMsg) => {
      if (msg.sessionId !== sessionId) return
      setRotations((prev) => [...prev, { from: msg.fromAccount, to: msg.toAccount, reason: msg.reason }])
    }
    // I3 — surface direct `error` frames (SESSION_NOT_FOUND, frame_too_large, etc.)
    const onError = (msg: WsErrorMsg) => {
      dispatch({ type: 'error', code: msg.code, message: msg.message, resetsAt: msg.resetsAt ?? null })
    }

    on('sessionEvent', onEvent)
    on('rotated', onRotated)
    on('error', onError)
    attach(sessionId)

    return () => {
      off('sessionEvent', onEvent)
      off('rotated', onRotated)
      off('error', onError)
      detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // aria-live mirror: announce latest assistant text after replay completes.
  useEffect(() => {
    if (state.replaying) {
      prevReplayingRef.current = true
      return
    }
    // If we just finished replaying, announce that.
    if (prevReplayingRef.current) {
      prevReplayingRef.current = false
      setAriaAnnouncement('History loaded')
      return
    }
    // Find the last assistant text block and mirror it.
    const lastAssistantTurn = [...state.turns].reverse().find((t) => t.role === 'assistant')
    if (!lastAssistantTurn) return
    const lastTextBlock = [...lastAssistantTurn.blocks].reverse().find((b) => b.kind === 'text')
    if (lastTextBlock && lastTextBlock.kind === 'text') {
      // Mirror last ~200 chars (enough for a screen reader without flooding)
      setAriaAnnouncement(lastTextBlock.text.slice(-200))
    }
  }, [state.turns, state.replaying])

  // Decision handler for permission cards
  const onDecision = useCallback(
    (permId: string, decision: 'allow' | 'deny' | 'always') => {
      permissionResponse(sessionId, permId, decision)
      dispatch({ type: '_resolvePermission', permId })
    },
    [sessionId, permissionResponse],
  )

  // Answer handler for question cards
  const onAnswer = useCallback(
    (questionId: string, selected: string[]) => {
      answerQuestion(sessionId, questionId, selected)
      dispatch({ type: '_resolveQuestion', questionId })
    },
    [sessionId, answerQuestion],
  )

  // Composer handlers
  const onPrompt = useCallback(
    (text: string) => prompt(sessionId, text),
    [sessionId, prompt],
  )
  const onInterrupt = useCallback(
    () => sendInterrupt(sessionId),
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

  const title = state.title ?? sessionId.slice(0, 8)

  // ── Status-strip derived data ──────────────────────────────────────────────
  // Account: the session record's account, overridden by the latest rotation.
  const { data: sessions } = useSessions()
  const { data: accounts } = useAccounts()
  const sessionRecord = sessions?.find((s) => s.id === sessionId)
  const latestRotation = rotations.length > 0 ? rotations[rotations.length - 1].to : null
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
    rotations,
    connectionState,
    title,
    account,
    quota,
    contextPct,
    subAgents,
    hasOpenAction,
    ariaAnnouncement,
    onDecision,
    onAnswer,
    onPrompt,
    onInterrupt,
    onSetMode,
    onCancelQueued,
  }
}
