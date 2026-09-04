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
import { Archive, MoreVertical, Pencil, RefreshCw, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCloseSession, usePermanentDeleteSession } from '@/api/hooks'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
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
}

export function SessionActions({ sessionId, sessionName, onDone, variant = 'dark', onRename, onReload }: Props) {
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
    'inline-flex items-center justify-center rounded-md min-h-[44px] min-w-[44px]',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
    isDark
      ? 'text-gray-200 hover:bg-gray-700 active:bg-gray-600'
      : 'text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700',
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
      <button
        type="button"
        onClick={handleArchive}
        disabled={closeSession.isPending}
        aria-label={`Archive session ${sessionName}`}
        className={iconBtn}
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
              'absolute right-0 top-full z-10 mt-1 w-44',
              'rounded-md border border-gray-200 dark:border-gray-700',
              'bg-white dark:bg-gray-900 shadow-lg py-1',
            )}
          >
            {onRename && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onRename() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-gray-700 dark:text-gray-200',
                  'hover:bg-gray-100 dark:hover:bg-gray-800',
                  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
                )}
              >
                <Pencil size={16} aria-hidden="true" />
                Rename
              </button>
            )}
            {onReload && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); onReload() }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                  'text-gray-700 dark:text-gray-200',
                  'hover:bg-gray-100 dark:hover:bg-gray-800',
                  'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
                )}
              >
                <RefreshCw size={16} aria-hidden="true" />
                Reload session
              </button>
            )}
            <button
              type="button"
              role="menuitem"
              onClick={() => { setMenuOpen(false); setConfirmOpen(true) }}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
                'text-red-600 dark:text-red-400',
                'hover:bg-gray-100 dark:hover:bg-gray-800',
                'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
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
          sessionName={sessionName}
          onClose={() => setConfirmOpen(false)}
          onConfirm={handleDeleteConfirm}
          isPending={deletePending}
        />
      )}

    </div>
  )
}
