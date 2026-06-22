/**
 * SessionView — rich session UI: transcript + composer + status (mobile / full-screen).
 *
 * The live session runtime (socket wiring, transcript reducer, derived status,
 * action handlers) lives in useSessionRuntime so it can be shared with the desktop
 * master-detail layout. SessionView owns only the full-screen chrome: the top bar
 * (Back, title, tool-detail toggle, status), the Esc/Shift+Tab/c keyboard
 * shortcuts, and the composer.
 *
 * Accessibility note:
 *   - An `aria-live="polite"` sr-only region (rendered by SessionBody) mirrors the
 *     latest assistant text so screen readers receive a summary.
 *   - Errors use `role="alert"` (assertive).
 */

import { useEffect, useCallback, useRef } from 'react'
import { cn } from '@/lib/utils'
import { useSessionSocket } from '@/ws/client'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { Composer } from './Composer'
import { SessionBody } from './SessionBody'
import { PendingQuestionPanel } from './PendingQuestionPanel'

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
  initialPrompt?: string
}

export function SessionView({ sessionId, onBack, initialPrompt }: Props) {
  const { detach } = useSessionSocket()
  const rt = useSessionRuntime(sessionId, initialPrompt)

  // Keep modeRef current so the keydown handler reads the latest mode without
  // needing to re-register the listener on every mode change.
  const modeRef = useRef(rt.mode)
  useEffect(() => {
    modeRef.current = rt.mode
  }, [rt.mode])

  const handleBack = useCallback(() => {
    detach(sessionId)
    onBack()
  }, [sessionId, onBack, detach])

  // In-session keyboard shortcuts:
  //   Esc  → back to session list (guard: skip if composer textarea has text mid-draft,
  //           or if a permission/question card is pending — let user act on the card first)
  //   Shift+Tab → cycle permission mode through PERMISSION_MODES
  //   c/C → toggle tool-detail mode (collapse/expand all tool calls)
  const { hasOpenAction, onSetMode, toggleToolDetail } = rt
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
        onSetMode(next)
      } else if (e.key === 'Escape') {
        // Guard 1: if the composer textarea (aria-label="Message") is focused and
        // has non-empty text, Escape is for the composer (clearing a draft) — do
        // NOT navigate back.
        const ae2 = document.activeElement as HTMLTextAreaElement | null
        const inComposerWithText =
          ae2?.getAttribute('aria-label') === 'Message' && (ae2?.value ?? '') !== ''
        if (inComposerWithText) return
        // Guard 2: if a permission/question card is pending, Escape is a no-op.
        if (hasOpenAction) return
        handleBack()
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSetMode, handleBack, hasOpenAction, toggleToolDetail])

  const toolToggleText =
    rt.toolDetailMode === 'auto' ? 'Collapse tools'
    : rt.toolDetailMode === 'all-closed' ? 'Expand tools'
    : 'Auto-fold'
  const toolToggleAria =
    rt.toolDetailMode === 'auto' ? 'Collapse tool calls'
    : rt.toolDetailMode === 'all-closed' ? 'Expand tool calls'
    : 'Auto-fold tool calls'

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

        <span className="text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

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
      </div>

      {/* Transcript + status strip + banners */}
      <SessionBody rt={rt} />

      {/* Pinned answer panel for the live open question (answer in place) */}
      <PendingQuestionPanel
        turns={rt.state.turns}
        onAnswer={rt.onAnswer}
        onChat={rt.onChat}
        armedQuestionId={rt.armedQuestionId}
      />

      {/* Composer */}
      <div className="flex-shrink-0">
        <Composer
          status={rt.state.status}
          mode={rt.mode}
          onPrompt={rt.onPrompt}
          onInterrupt={rt.onInterrupt}
          onSetMode={rt.onSetMode}
          queued={rt.state.queued}
          onCancelQueued={rt.onCancelQueued}
          chatArmed={rt.chatArmed}
          autoFocusKey={sessionId}
        />
      </div>
    </div>
  )
}
