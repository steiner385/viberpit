/**
 * The in-session keyboard gestures — one implementation, every layout mode.
 *
 * ## Where this came from
 *
 * Two window-level `keydown` handlers used to implement the same gestures:
 * `SessionView.tsx` (phone, full screen) and `DesktopShell.tsx` (desktop,
 * master-detail). They had already drifted — the desktop copy had no
 * pending-card guard, so Escape over an unanswered permission card detached
 * instead of letting the card handle it, and it had no Shift+Tab at all. Task 17
 * deleted `DesktopShell` and Task 18 deletes `SessionView` (the shell renders
 * `MainPaneSession` in every mode now), so the surviving copy is here, and it is
 * the STRICTER of the two: the guard that was the phone's is now everyone's.
 *
 * ## Why it is still a window listener
 *
 * The gestures have to fire while focus is inside the transcript, a tool card, a
 * question card or the composer — anywhere in the session, including nodes that
 * portal out of the pane. `detachKeyYieldsToTextEntry` is what keeps that from
 * hijacking typing: the composer keeps its left-edge ← contract, and every other
 * text entry holds all three keys while it has text. PR 4 replaces this with the
 * shortcuts registry; until then this is the one owner (`lib/shortcuts.ts` says
 * so beside the rows it documents).
 */
import { useCallback, useEffect, useRef } from 'react'
import { detachKeyYieldsToTextEntry } from '@/lib/detachKeyGuard'
import { PERMISSION_MODES } from '@/session/permissionModes'

/**
 * Cycle to the next permission mode in PERMISSION_MODES order.
 * Wraps around: full-auto → interactive.
 */
export function nextPermissionMode(current: string): string {
  const values: string[] = PERMISSION_MODES.map((m) => m.value)
  const i = values.indexOf(current)
  return values[(i + 1) % values.length]
}

export interface InSessionKeysOptions {
  /** The live permission mode, read fresh on each keystroke. */
  mode: string
  /**
   * A permission or question card is waiting for an answer. Esc and ← yield to
   * it — answer the card first. Ctrl+Z deliberately does NOT: it is the
   * force-detach escape hatch.
   */
  hasOpenAction: boolean
  /** Detach and leave the session (the click equivalent is the Back control). */
  onDetach: () => void
  /** Apply a new permission mode (Shift+Tab). */
  onSetMode: (mode: string) => void
}

export function useInSessionKeys({ mode, hasOpenAction, onDetach, onSetMode }: InSessionKeysOptions): void {
  // Keep the mode in a ref so the handler reads the latest value without
  // re-registering the listener on every mode change.
  const modeRef = useRef(mode)
  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  const detach = useCallback(() => onDetach(), [onDetach])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Tab' && e.shiftKey) {
        e.preventDefault()
        onSetMode(nextPermissionMode(modeRef.current))
        return
      }
      const active = document.activeElement
      // Ctrl+Z → force-detach escape hatch (bypasses the pending-card guard).
      if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) {
        if (detachKeyYieldsToTextEntry('CtrlZ', active)) return
        e.preventDefault()
        detach()
        return
      }
      if (e.key === 'Escape') {
        if (detachKeyYieldsToTextEntry('Escape', active)) return
        if (hasOpenAction) return
        e.preventDefault()
        detach()
        return
      }
      // ← detaches only from the prompt's left edge; see detachKeyGuard.
      if (e.key === 'ArrowLeft') {
        if (detachKeyYieldsToTextEntry('ArrowLeft', active)) return
        if (hasOpenAction) return
        e.preventDefault()
        detach()
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [detach, hasOpenAction, onSetMode])
}
