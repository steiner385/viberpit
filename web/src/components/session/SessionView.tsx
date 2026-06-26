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
import { useSessionNeighbors } from '@/session/useSessionNeighbors'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { SessionComposer } from './SessionComposer'
import { SessionBody } from './SessionBody'
import { CommandResultOverlay } from './CommandResultOverlay'
import { SessionActions } from '@/components/session/SessionActions'
import { StatusPill } from '@/components/UsageBadge'
import type { SessionStatus } from '@/api/types'

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
  /** Called after archive/delete — host advances to the neighbor (distinct from onBack). */
  onDone: () => void
  onNavigateSession?: (id: string) => void
  initialPrompt?: string
}

export function SessionView({ sessionId, onBack, onDone, onNavigateSession, initialPrompt }: Props) {
  const { detach } = useSessionSocket()
  const rt = useSessionRuntime(sessionId, initialPrompt)
  const { prevId, nextId } = useSessionNeighbors(sessionId)
  // Both chevrons render as a pair: useSessionNeighbors returns prevId/nextId
  // both-null (single/unknown session) or both-set (wraparound), never one null.
  const hasNeighbors = prevId !== null || nextId !== null

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

  const handleDone = useCallback(() => {
    detach(sessionId)
    onDone()
  }, [sessionId, onDone, detach])

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
      {/*
        Top bar — two rows. Row 1 carries only the low-width navigation chrome
        (Back, prev/next chevrons) plus the title, so the greedy `flex-1` title
        actually has room to expand instead of collapsing to an ellipsis on a
        narrow phone. Row 2 holds the status pill and the archive/⋮ actions,
        moved down out of the title's way and right-aligned so they stay one
        glance from the title.
      */}
      <div className={cn(
        'flex flex-col gap-2 px-3 py-2',
        'bg-gray-900 border-b border-gray-700',
        'flex-shrink-0',
      )}>
        {/* Row 1: navigation + title */}
        <div className="flex items-center gap-3">
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

          {hasNeighbors && (
            <button
              type="button"
              onClick={() => prevId && onNavigateSession?.(prevId)}
              aria-label="Previous session"
              className={cn(
                'flex items-center justify-center px-2 py-2 rounded-md min-h-[44px] min-w-[44px]',
                'text-lg leading-none font-medium text-gray-200',
                'hover:bg-gray-700 active:bg-gray-600',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              ‹
            </button>
          )}

          <span className="flex-1 text-sm font-semibold text-gray-100 truncate">{rt.title}</span>

          {hasNeighbors && (
            <button
              type="button"
              onClick={() => nextId && onNavigateSession?.(nextId)}
              aria-label="Next session"
              className={cn(
                'flex items-center justify-center px-2 py-2 rounded-md min-h-[44px] min-w-[44px]',
                'text-lg leading-none font-medium text-gray-200',
                'hover:bg-gray-700 active:bg-gray-600',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              ›
            </button>
          )}
        </div>

        {/* Row 2: status + actions, moved off the title row */}
        <div className="flex items-center justify-end gap-3">
          {/*
            The same compact status pill the session card renders. Reads the live
            runtime status (`rt.state.status.state`) and routes it through the shared
            statusPresentation vocabulary, so label/icon/color stay identical to the
            card.
          */}
          <StatusPill status={rt.state.status.state as SessionStatus} className="flex-shrink-0" />

          <SessionActions
            sessionId={sessionId}
            sessionName={rt.title}
            onDone={handleDone}
            variant="dark"
            canRename
          />
        </div>
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
