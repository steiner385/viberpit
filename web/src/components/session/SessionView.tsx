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
import { selectSubAgents } from '@/session/selectors'
import { remainingContextPct } from '@/lib/contextWindow'
import { useSessions, useAccounts } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { StatusBar } from './StatusBar'
import { SessionMeta } from './SessionMeta'
import { TranscriptView } from './TranscriptView'
import { Composer } from './Composer'
import { WorkingIndicator } from './WorkingIndicator'

/**
 * Cycle to the next permission mode in PERMISSION_MODES order.
 * Wraps around: full-auto → interactive.
 */
function nextPermissionMode(current: string): string {
  const values: string[] = PERMISSION_MODES.map((m) => m.value)
  const i = values.indexOf(current)
  return values[(i + 1) % values.length]
}

interface Props {
  sessionId: string
  onBack: () => void
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
  const modeRef = useRef('interactive')
  const [toolDetailMode, setToolDetailMode] = useState<ToolDetailMode>('auto')
  const toggleToolDetail = useCallback(
    () => setToolDetailMode((m) =>
      m === 'auto' ? 'all-closed' : m === 'all-closed' ? 'all-open' : 'auto',
    ),
    [],
  )

  // Track whether replay just finished so we can announce "History loaded" once.
  const prevReplayingRef = useRef(true)
  // The aria-live region is an external system (the screen reader); we write its
  // text imperatively via this ref rather than through React state, so the effect
  // below has no setState and triggers no extra render.
  const liveRef = useRef<HTMLDivElement>(null)

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
    attach(sessionId)

    return () => {
      off('sessionEvent', onEvent)
      off('rotated', onRotated)
      off('error', onError)
      detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // aria-live mirror: write the announcement into the sr-only region (an external
  // system) imperatively, so there's no setState in this effect.
  useEffect(() => {
    const el = liveRef.current
    if (!el) return
    if (state.replaying) {
      prevReplayingRef.current = true
      return
    }
    // If we just finished replaying, announce that.
    if (prevReplayingRef.current) {
      prevReplayingRef.current = false
      el.textContent = 'History loaded'
      return
    }
    // Find the last assistant text block and mirror it.
    const lastAssistantTurn = [...state.turns].reverse().find((t) => t.role === 'assistant')
    if (!lastAssistantTurn) return
    const lastTextBlock = [...lastAssistantTurn.blocks].reverse().find((b) => b.kind === 'text')
    if (lastTextBlock && lastTextBlock.kind === 'text') {
      // Mirror last ~200 chars (enough for a screen reader without flooding)
      el.textContent = lastTextBlock.text.slice(-200)
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

  const handleBack = useCallback(() => {
    detach(sessionId)
    onBack()
  }, [sessionId, onBack, detach])

  // Keep modeRef current so the keydown handler reads the latest mode without
  // needing to re-register the listener on every mode change.
  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  // Determine if any open permission/question block exists.
  // Declared before the keydown effect so the effect closure captures the current value.
  const hasOpenAction = state.turns.some((t) =>
    t.blocks.some((b) => b.kind === 'permission' || b.kind === 'question'),
  )

  // In-session keyboard shortcuts (Task 7):
  //   Esc  → back to session list (guard: skip if composer textarea has text mid-draft,
  //           or if a permission/question card is pending — let user act on the card first)
  //   Shift+Tab → cycle permission mode through PERMISSION_MODES
  //   c/C → toggle tool-detail mode (collapse/expand all tool calls)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const ae = document.activeElement as HTMLElement | null
      const typing = !!ae && (ae.tagName === 'TEXTAREA' || ae.tagName === 'INPUT' || ae.isContentEditable)
      if (!typing && (e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        toggleToolDetail()
        return
      }
      if (e.key === 'Tab' && e.shiftKey) {
        e.preventDefault()
        const next = nextPermissionMode(modeRef.current)
        handleSetMode(next)
      } else if (e.key === 'Escape') {
        // Guard 1: if the composer textarea (aria-label="Message") is focused and
        // has non-empty text, Escape is for the composer (clearing a draft) — do
        // NOT navigate back. Navigate back only when the textarea is empty or
        // focus is elsewhere.
        const ae2 = document.activeElement as HTMLTextAreaElement | null
        const inComposerWithText =
          ae2?.getAttribute('aria-label') === 'Message' && (ae2?.value ?? '') !== ''
        if (inComposerWithText) return
        // Guard 2: if a permission/question card is pending, Escape is a no-op —
        // the user must respond to the card before navigating away.
        if (hasOpenAction) return
        handleBack()
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [handleSetMode, handleBack, hasOpenAction, toggleToolDetail])

  const title = state.title ?? sessionId.slice(0, 8)

  const toolToggleText =
    toolDetailMode === 'auto' ? 'Collapse tools'
    : toolDetailMode === 'all-closed' ? 'Expand tools'
    : 'Auto-fold'
  const toolToggleAria =
    toolDetailMode === 'auto' ? 'Collapse tool calls'
    : toolDetailMode === 'all-closed' ? 'Expand tool calls'
    : 'Auto-fold tool calls'

  // ── Status-strip derived data ──────────────────────────────────────────────
  // Account: the session record's account, overridden by the latest rotation.
  const { data: sessions } = useSessions()
  const { data: accounts } = useAccounts()
  const sessionRecord = sessions?.find((s) => s.id === sessionId)
  const latestRotation =
    state.rotations.length > 0 ? state.rotations[state.rotations.length - 1].to : null
  const account = latestRotation ?? sessionRecord?.accountName ?? '—'

  const accountUsage = accounts?.find((a) => a.name === account)?.usage ?? null
  const quota = accountUsage
    ? { sessionPct: accountUsage.session.pct, weeklyPct: accountUsage.weekly.pct }
    : null

  const contextPct = remainingContextPct(state.usage, state.model)
  const subAgents = selectSubAgents(state.turns)

  return (
    <div className="flex flex-col h-dvh bg-white dark:bg-gray-950">
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

        <button
          type="button"
          onClick={toggleToolDetail}
          aria-label={toolToggleAria}
          className={cn(
            'ml-auto flex items-center gap-1.5 px-3 py-2 rounded-md min-h-[44px]',
            'text-sm font-medium text-gray-200',
            'hover:bg-gray-700 active:bg-gray-600',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          {toolToggleText}
        </button>

        <div>
          <StatusBar status={state.status} connectionState={connectionState} />
        </div>
      </div>

      {/* Status strip: account · model · context · quota · sub-agents */}
      <div className="flex-shrink-0">
        <SessionMeta
          account={account}
          model={state.model}
          contextPct={contextPct}
          quota={quota}
          subAgents={subAgents}
        />
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

      {/* Transcript — rotation dividers are interleaved inline by anchor turn */}
      <TranscriptView
        turns={state.turns}
        rotations={state.rotations}
        permissionMode={mode}
        toolDetailMode={toolDetailMode}
        onDecision={handleDecision}
        onAnswer={handleAnswer}
      />

      {/* Prominent "waiting for Claude" indicator shown below the last turn */}
      {state.status.state === 'working' && (
        <div
          aria-hidden="true"
          className={cn(
            'flex-shrink-0 flex items-center px-4 py-2',
            'text-xs text-gray-400 dark:text-gray-500',
          )}
        >
          <WorkingIndicator count={state.status.count} />
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
       * Content is written imperatively via liveRef (see the effect above) — kept
       * childless in JSX so React doesn't clobber the imperatively-set text.
       */}
      <div
        ref={liveRef}
        aria-live="polite"
        aria-atomic="false"
        aria-label="Session output"
        className="sr-only"
      />
    </div>
  )
}
