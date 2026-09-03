/**
 * The in-session keyboard gestures — one implementation, every layout mode.
 *
 * ## Where this came from
 *
 * Two window-level `keydown` handlers used to implement the same gestures:
 * `SessionView.tsx` (phone, full screen) and `DesktopShell.tsx` (desktop,
 * master-detail). They had already drifted — the desktop copy had no
 * pending-card guard, so Escape over an unanswered permission card detached
 * instead of letting the card handle it. Task 17 deleted `DesktopShell` and
 * Task 18 deletes `SessionView` (the shell renders `MainPaneSession` in every
 * mode now), so the surviving copy is here, and it is the STRICTER of the two:
 * the guard that was the phone's is now everyone's.
 *
 * ## Why `Shift+Tab` is NOT here
 *
 * `SessionView`'s copy also cycled the permission mode on `Shift+Tab`, ahead of
 * the guard and with an unconditional `preventDefault()`. That was survivable
 * while `SessionView` was a chrome-free full-screen route whose only focusables
 * were its own. It is not survivable here: this hook now mounts inside
 * `AppShell`, beside a focusable rail, bar, dock and drawer, so the branch
 * killed reverse-tab navigation for the whole document and walked a LIVE
 * session's permission mode toward `bypassPermissions` — including
 * `full-auto` — whenever the user pressed the platform gesture for "move focus
 * backwards". Sub-spec B **D8** removes the binding anyway (PR 4's shortcuts
 * registry has no row for it, and `shell/shortcuts.ts` already dropped the help
 * text on that basis), so the cutover brings the removal forward rather than
 * broadening the binding into every layout mode. Permission mode changes go
 * through the mode selector in `SessionMeta`, which is confirmed and visible.
 *
 * ## Why it is still a window listener
 *
 * The gestures have to fire while focus is inside the transcript, a tool card, a
 * question card or the composer — anywhere in the session, including nodes that
 * portal out of the pane. `detachKeyYieldsToTextEntry` is what keeps that from
 * hijacking typing: the composer keeps its left-edge ← contract, and every other
 * text entry holds all three keys while it has text. PR 4 replaces this with the
 * shortcuts registry; until then this is the one owner (`shell/shortcuts.ts` says
 * so beside the rows it documents).
 */
import { useCallback, useEffect } from 'react'
import { detachKeyYieldsToTextEntry } from '@/lib/detachKeyGuard'

export interface InSessionKeysOptions {
  /**
   * A permission or question card is waiting for an answer. Esc and ← yield to
   * it — answer the card first. Ctrl+Z deliberately does NOT: it is the
   * force-detach escape hatch.
   */
  hasOpenAction: boolean
  /** Detach and leave the session (the click equivalent is the Back control). */
  onDetach: () => void
}

export function useInSessionKeys({ hasOpenAction, onDetach }: InSessionKeysOptions): void {
  const detach = useCallback(() => onDetach(), [onDetach])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
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
  }, [detach, hasOpenAction])
}
