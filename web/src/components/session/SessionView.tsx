/**
 * SessionView — rich session UI: transcript + composer + status (mobile / full-screen).
 *
 * The live session runtime (socket wiring, transcript reducer, derived status,
 * action handlers) lives in useSessionRuntime so it can be shared with the desktop
 * master-detail layout. SessionView owns only the full-screen chrome: the top bar
 * with Back, the Esc/Shift+Tab keyboard shortcuts, and the composer.
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
import { StatusBar } from './StatusBar'
import { Composer } from './Composer'
import { SessionBody } from './SessionBody'

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
  const { detach } = useSessionSocket()
  const rt = useSessionRuntime(sessionId)

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
  //   Esc  → back to session list (guarded: skip if composer textarea has text mid-draft,
  //           or if a permission/question card is pending — let user act on the card first)
  //   Shift+Tab → cycle permission mode through PERMISSION_MODES
  const { hasOpenAction, onSetMode } = rt
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Tab' && e.shiftKey) {
        e.preventDefault()
        const next = nextPermissionMode(modeRef.current)
        onSetMode(next)
      } else if (e.key === 'Escape') {
        // Guard 1: if the composer textarea (aria-label="Message") is focused and
        // has non-empty text, Escape is for the composer (clearing a draft) — do
        // NOT navigate back.
        const ae = document.activeElement as HTMLTextAreaElement | null
        const inComposerWithText =
          ae?.getAttribute('aria-label') === 'Message' && (ae?.value ?? '') !== ''
        if (inComposerWithText) return
        // Guard 2: if a permission/question card is pending, Escape is a no-op.
        if (hasOpenAction) return
        handleBack()
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSetMode, handleBack, hasOpenAction])

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

        <span className="text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

        <div className="ml-auto">
          <StatusBar status={rt.state.status} connectionState={rt.connectionState} />
        </div>
      </div>

      {/* Transcript + status strip + banners */}
      <SessionBody rt={rt} />

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
        />
      </div>
    </div>
  )
}
