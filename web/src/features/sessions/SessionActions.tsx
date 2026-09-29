/**
 * SessionActions — in-session Archive + Delete (+ optional Rename) controls for the
 * session header.
 *
 * Archive is a primary header button (reversible → no confirm). Delete lives behind an
 * overflow "⋯" menu and is destructive → it confirms via the shared PermanentDeleteDialog.
 * Delete is archive-then-delete: archive first (stops the live driver, moves the dir to the
 * archive), and only on success permanently delete — so we never leave a zombie driver. On a
 * successful archive or delete we call onDone() so the host detaches the socket and navigates
 * back to the list. Used by both session headers: session/SessionView.tsx (mobile, dark bar)
 * and DesktopShell's MainPaneSession (desktop, light bar). The dropdown mirrors ExportMenu's
 * lightweight role="menu" pattern.
 *
 * Rename is opt-in via an `onRename` CALLBACK, not a `canRename` flag: since Task 10 the
 * rename flow (state + the shared `features/sessions/RenameDialog` + `useRenameSession`)
 * lives once, in `SessionHeader`, which drives it from two triggers — the click-to-rename
 * title and this menu item. Owning a second copy of that dialog here is the
 * parallel-implementation anti-pattern, so the copy that used to live here is gone.
 * Callers that pass no `onRename` get a delete-only menu, exactly as before.
 */
import { useEffect, useRef, useState } from 'react'
import { Archive, MoreVertical, Pencil, RefreshCw, Shuffle, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCloseSession, usePermanentDeleteSession } from '@/api/hooks'
import { PermanentDeleteDialog } from '@/features/sessions/list/PermanentDeleteDialog'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'

interface Props {
  sessionId: string
  sessionName: string
  /** Called after a successful archive or permanent delete (host detaches + navigates). */
  onDone: () => void
  /** Header colour scheme: 'dark' for the mobile bar (gray-900), 'light' for desktop. */
  variant?: 'dark' | 'light'
  /** Surface a Rename menu item that calls this. Omitted → the menu is delete-only. */
  onRename?: () => void
  /**
   * Surface a "Reload session" menu item that calls this — the quick recovery
   * for a session the UI can no longer act on (answers rejected, a turn that
   * stopped streaming). The host owns the mutation + socket re-attach, exactly
   * as `onRename` owns the rename flow. Omitted → no reload item.
   */
  onReload?: () => void
  /**
   * Surface a "Next best account" menu item that calls this — move the session to
   * the account the selector ranks best and continue the conversation. The host owns
   * the mutation + socket re-attach, like `onReload`. Omitted → no item.
   */
  onRotate?: () => void
}

export function SessionActions({ sessionId, sessionName, onDone, variant = 'dark', onRename, onReload, onRotate }: Props) {
  const closeSession = useCloseSession()
  const permanentDelete = usePermanentDeleteSession()
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuTriggerRef = useRef<HTMLButtonElement>(null)

  // Close the overflow menu on outside-click. Escape is the container's own
  // `onKeyDown` below (PR 4 removed the `document` keydown that used to be here:
  // the app has one keydown listener now, in `shell/useShortcuts`).
  useEffect(() => {
    if (!menuOpen) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [menuOpen])

  // An open menu is an overlay — the shell's shortcuts (← / Esc detach) do not
  // fire through one. Same registration `Dialog`, `Sheet` and `ui/Menu` make.
  useEffect(() => {
    if (!menuOpen) return
    pushOverlay()
    return () => popOverlay()
  }, [menuOpen])

  // Focus the trigger when the menu opens (same fix, same reason, as
  // `SessionCard`'s ⋯ menu): clicking a `<button>` does not focus it on macOS
  // Safari or Firefox, and the browser delivers keydown to
  // `document.activeElement` — so the container's Escape handler below would
  // never see the key, while the overlay push above keeps every global shortcut
  // suppressed. The trigger is the node the markup names as the menu's control
  // (`aria-haspopup` / `aria-expanded`) and it outlives the menu, so closing
  // needs no focus restoration.
  useEffect(() => {
    if (menuOpen) menuTriggerRef.current?.focus()
  }, [menuOpen])

  const isDark = variant === 'dark'
  const iconBtn = cn(
    'inline-flex items-center justify-center rounded-md min-h-target min-w-target',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
    isDark
      ? 'text-ink hover:bg-surface-raised active:bg-surface-raised'
      : 'text-ink-muted hover:bg-surface-raised',
  )

  function handleArchive() {
    onDone()
    closeSession.mutate(sessionId)
  }

  function handleDeleteConfirm() {
    // Archive-then-delete: stop + archive the live session, then permanently delete.
    closeSession.mutate(sessionId, {
      onSuccess: () => {
        permanentDelete.mutate(sessionId, {
          onSuccess: () => { setConfirmOpen(false); onDone() },
        })
      },
    })
  }

  const deletePending = closeSession.isPending || permanentDelete.isPending

  return (
    // Escape closes the menu from the container that holds the trigger and the
    // menu both, and stops the key there: dismissing a menu must not also detach
    // from the session the header belongs to.
    <div
      className="flex items-center gap-1"
      ref={menuRef}
      onKeyDown={(e) => {
        if (menuOpen && e.key === 'Escape') {
          e.stopPropagation()
          setMenuOpen(false)
        }
      }}
    >
      {/* Below `narrow:` Archive folds into the ⋯ menu (item below): on a 393px
          phone Back, ‹, ›, this button and ⋯ each held 44px and left the title
          six characters (2026-09-29 re-critique). Archive is reversible and one
          tap further away; the chevrons stay — they are the phone's only way
          between sessions. Hidden, not unmounted, so jsdom and the desktop keep
          the same control. */}
      <button
        type="button"
        onClick={handleArchive}
        disabled={closeSession.isPending}
        aria-label={`Archive session ${sessionName}`}
        className={cn(iconBtn, 'narrow:hidden')}
      >
        <Archive size={18} aria-hidden="true" />
      </button>

      <div className="relative">
        <button
          type="button"
          ref={menuTriggerRef}
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="More session actions"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className={iconBtn}
        >
          <MoreVertical size={18} aria-hidden="true" />
        </button>

        {menuOpen && (
          <div
            role="menu"
            aria-label="Session actions"
            className={cn(
              'absolute right-0 top-full z-sticky mt-1 w-44',
              'rounded-md border border-edge',
              'bg-surface shadow-lg py-1',
            )}
          >
            {onRename && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onRename() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-ink',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:bg-surface-raised',
                )}
              >
                <Pencil size={16} aria-hidden="true" />
                Rename
              </button>
            )}
            {/* The narrow-only home of Archive (see the header button above). */}
            <button
              type="button"
              role="menuitem"
              data-testid="session-actions-archive-item"
              disabled={closeSession.isPending}
              onClick={() => { setMenuOpen(false); handleArchive() }}
              className={cn(
                'hidden narrow:flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                'text-ink',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:bg-surface-raised',
              )}
            >
              <Archive size={16} aria-hidden="true" />
              Archive
            </button>
            {onReload && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onReload() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-ink',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:bg-surface-raised',
                )}
              >
                <RefreshCw size={16} aria-hidden="true" />
                Reload session
              </button>
            )}
            {onRotate && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onRotate() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-ink',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:bg-surface-raised',
                )}
              >
                <Shuffle size={16} aria-hidden="true" />
                Next best account
              </button>
            )}
            <button
              type="button"
              role="menuitem"
              onClick={() => { setMenuOpen(false); setConfirmOpen(true) }}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                'text-danger',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:bg-surface-raised',
              )}
            >
              <Trash2 size={16} aria-hidden="true" />
              Delete permanently
            </button>
          </div>
        )}
      </div>

      {confirmOpen && (
        <PermanentDeleteDialog
          names={[sessionName]}
          onClose={() => setConfirmOpen(false)}
          onConfirm={handleDeleteConfirm}
          isPending={deletePending}
        />
      )}

    </div>
  )
}
