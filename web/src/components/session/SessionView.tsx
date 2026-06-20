/**
 * SessionView — rich session UI: transcript + composer + status.
 *
 * Accessibility note:
 *   - An `aria-live="polite"` sr-only region mirrors the latest assistant text
 *     so screen readers receive a summary without being overwhelmed.
 *   - Announcements are suppressed while `state.replaying` is true; once
 *     replayComplete fires, a single "History loaded" notice is announced.
 *   - Errors use `role="alert"` (assertive).
 */

import { useEffect, useReducer, useState, useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import { useSessionSocket } from '@/ws/client'
import type { WsRotatedMsg, WsErrorMsg } from '@/ws/client'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { SessionEvent } from '@/session/events'
import { StatusBar } from './StatusBar'
import { RotationDivider } from './RotationDivider'
import { TranscriptView } from './TranscriptView'
import { Composer } from './Composer'

interface Props {
  sessionId: string
  onBack: () => void
}

interface Rotation {
  from: string
  to: string
  reason?: string
}

export function SessionView({ sessionId, onBack }: Props) {
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
  const handleDecision = useCallback(
    (permId: string, decision: 'allow' | 'deny' | 'always') => {
      permissionResponse(sessionId, permId, decision)
      dispatch({ type: '_resolvePermission', permId })
    },
    [sessionId, permissionResponse],
  )

  // Answer handler for question cards
  const handleAnswer = useCallback(
    (questionId: string, selected: string[]) => {
      answerQuestion(sessionId, questionId, selected)
      dispatch({ type: '_resolveQuestion', questionId })
    },
    [sessionId, answerQuestion],
  )

  // Composer handlers
  const handlePrompt = useCallback(
    (text: string) => prompt(sessionId, text),
    [sessionId, prompt],
  )
  const handleInterrupt = useCallback(
    () => sendInterrupt(sessionId),
    [sessionId, sendInterrupt],
  )
  const handleSetMode = useCallback(
    (m: string) => {
      setMode(m)
      setPermissionMode(sessionId, m)
    },
    [sessionId, setPermissionMode],
  )

  function handleBack() {
    detach(sessionId)
    onBack()
  }

  // Determine if any open permission/question block exists
  const hasOpenAction = state.turns.some((t) =>
    t.blocks.some((b) => b.kind === 'permission' || b.kind === 'question'),
  )

  const title = state.title ?? sessionId.slice(0, 8)

  return (
    <div className="flex flex-col h-screen bg-white dark:bg-gray-950">
      {/* Top bar */}
      <div className={cn(
        'flex items-center gap-3 px-3 py-2',
        'bg-gray-900 border-b border-gray-700',
        'flex-shrink-0',
      )}>
        <button
          type="button"
          onClick={handleBack}
          aria-label="Back to session list"
          className={cn(
            'flex items-center gap-1.5 px-3 py-2 rounded-md min-h-[44px]',
            'text-sm font-medium text-gray-200',
            'hover:bg-gray-700 active:bg-gray-600',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          ← Back
        </button>

        <span className="text-sm font-semibold text-gray-100 truncate">{title}</span>

        <div className="ml-auto">
          <StatusBar status={state.status} connectionState={connectionState} />
        </div>
      </div>

      {/* Error banner */}
      {state.error && (
        <div
          role="alert"
          className={cn(
            'flex-shrink-0 px-4 py-3 text-sm',
            'bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-300',
            'border-b border-red-200 dark:border-red-800',
          )}
        >
          <span className="font-medium">Error: </span>
          {state.error.message}
          {state.error.resetsAt && (
            <span className="ml-2 text-xs opacity-75">
              (Resets at {new Date(state.error.resetsAt).toLocaleTimeString()})
            </span>
          )}
        </div>
      )}

      {/* Transcript */}
      <TranscriptView
        turns={state.turns}
        onDecision={handleDecision}
        onAnswer={handleAnswer}
      />

      {/* Rotation dividers (appended after transcript content) */}
      {rotations.length > 0 && (
        <div className="flex-shrink-0 px-4">
          {rotations.map((r, i) => (
            <RotationDivider key={i} from={r.from} to={r.to} reason={r.reason} />
          ))}
        </div>
      )}

      {/* "Action needed" sticky banner when permission/question is pending */}
      {hasOpenAction && (
        <div
          aria-live="polite"
          className={cn(
            'flex-shrink-0 px-4 py-2 text-xs font-medium',
            'bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300',
            'border-t border-amber-200 dark:border-amber-800',
          )}
        >
          Action needed — scroll up to respond
        </div>
      )}

      {/* Composer */}
      <div className="flex-shrink-0">
        <Composer
          status={state.status}
          mode={mode}
          onPrompt={handlePrompt}
          onInterrupt={handleInterrupt}
          onSetMode={handleSetMode}
          queued={state.queued}
          onCancelQueued={(i) => cancelQueued(sessionId, i)}
        />
      </div>

      {/*
       * aria-live mirror (sr-only).
       * Announces the latest assistant text (debounced implicitly by state updates).
       * Suppressed while replaying; announces "History loaded" once on replayComplete.
       */}
      <div
        aria-live="polite"
        aria-atomic="false"
        aria-label="Session output"
        className="sr-only"
      >
        {ariaAnnouncement}
      </div>
    </div>
  )
}
