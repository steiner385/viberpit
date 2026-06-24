/**
 * SessionView — rich session UI: transcript + composer + status (mobile / full-screen).
 *
 * The live session runtime (socket wiring, transcript reducer, derived status,
 * action handlers) lives in useSessionRuntime so it can be shared with the desktop
 * master-detail layout. SessionView owns only the full-screen chrome: the top bar
 * (Back, title, status), the Esc/Shift+Tab keyboard shortcuts, and the composer.
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
import { SessionComposer } from './SessionComposer'
import { SessionBody } from './SessionBody'
import { CommandResultOverlay } from './CommandResultOverlay'
import { SessionActions } from '@/components/session/SessionActions'

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
  //   ←    → back to session list, but only from the prompt's left edge: empty composer,
  //           or caret at the very start with no selection. Otherwise ← moves the caret.
  //   Shift+Tab → cycle permission mode through PERMISSION_MODES
  const { hasOpenAction, onSetMode } = rt
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Tab' && e.shiftKey) {
        e.preventDefault()
        const next = nextPermissionMode(modeRef.current)
        onSetMode(next)
        return
      }
      const ae2 = document.activeElement as HTMLTextAreaElement | null
      const inComposer = ae2?.getAttribute('aria-label') === 'Message'
      // Esc/Ctrl+Z yield to the composer whenever it holds a draft (keep the text).
      const inComposerWithText = inComposer && (ae2?.value ?? '') !== ''
      // ← yields to the composer only when the caret is past the start or text is
      // selected; from the left edge (or an empty prompt) it detaches to the list.
      const caretPastComposerStart =
        inComposer && !(ae2!.selectionStart === 0 && ae2!.selectionEnd === 0)
      // Ctrl+Z → unconditional force-detach escape hatch (bypasses the pending-card guard).
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (inComposerWithText) return
        e.preventDefault()
        handleBack()
        return
      }
      // Esc → detach to the list. Guards: composer-with-text, and a pending
      // permission/question card (answer it first).
      if (e.key === 'Escape') {
        if (inComposerWithText) return
        if (hasOpenAction) return
        e.preventDefault()
        handleBack()
        return
      }
      // ← (from the prompt's left edge) → detach to the list. Same pending-card guard.
      if (e.key === 'ArrowLeft') {
        if (caretPastComposerStart) return
        if (hasOpenAction) return
        e.preventDefault()
        handleBack()
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSetMode, handleBack, hasOpenAction])

  return (
    <div className="flex flex-col h-dvh bg-white dark:bg-gray-950">
      <CommandResultOverlay
        result={rt.commandResult}
        commands={rt.commands}
        onClose={rt.dismissCommandResult}
      />
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

        <span className="flex-1 text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

        <SessionActions
          sessionId={sessionId}
          sessionName={rt.title}
          onDone={handleBack}
          variant="dark"
        />
      </div>

      {/* Transcript + status strip + banners + pinned live-question panel
          (the panel lives in SessionBody so desktop + mobile share it). */}
      <SessionBody rt={rt} />

      {/* Composer */}
      <div className="flex-shrink-0">
        <SessionComposer rt={rt} />
      </div>
    </div>
  )
}
