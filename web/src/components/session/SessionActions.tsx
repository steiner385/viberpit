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
 * Rename is opt-in via `canRename` and shows only on mobile (SessionView): the mobile
 * session card drops its per-row rename button to declutter, so the detail view is rename's
 * only home there. Desktop keeps rename on its always-visible sidebar card, so MainPaneSession
 * leaves `canRename` unset and the menu is delete-only — desktop is unchanged.
 */
import { useEffect, useRef, useState } from 'react'
import { Archive, MoreVertical, Pencil, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCloseSession, usePermanentDeleteSession, useRenameSession } from '@/api/hooks'
import { PermanentDeleteDialog } from '@/components/PermanentDeleteDialog'
import { RenameDialog } from '@/components/Dashboard'

interface Props {
  sessionId: string
  sessionName: string
  /** Called after a successful archive or permanent delete (host detaches + navigates). */
  onDone: () => void
  /** Header colour scheme: 'dark' for the mobile bar (gray-900), 'light' for desktop. */
  variant?: 'dark' | 'light'
  /** Surface a Rename menu item (mobile only — the mobile card has no rename button). */
  canRename?: boolean
}

export function SessionActions({ sessionId, sessionName, onDone, variant = 'dark', canRename = false }: Props) {
  const closeSession = useCloseSession()
  const permanentDelete = usePermanentDeleteSession()
  const renameSession = useRenameSession()
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [renameOpen, setRenameOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close the overflow menu on outside-click / Escape (mirrors ExportMenu).
  useEffect(() => {
    if (!menuOpen) return
    function onDocClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') { e.stopPropagation(); setMenuOpen(false) } }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
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
    closeSession.mutate(sessionId, { onSuccess: onDone })
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
    <div className="flex items-center gap-1" ref={menuRef}>
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
            {canRename && (
              <button
                type="button"
                role="menuitem"
                onClick={() => { setMenuOpen(false); setRenameOpen(true) }}
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

      {/* Rename reuses the shared dialog from Dashboard (same one the desktop
          sidebar card uses) — no parallel rename UI. Unlike archive/delete it
          stays in place (no onDone), so the header just refreshes its title. */}
      {renameOpen && (
        <RenameDialog
          sessionId={sessionId}
          currentName={sessionName}
          onClose={() => setRenameOpen(false)}
          onSubmit={(id, name) => renameSession.mutate({ id, name }, { onSuccess: () => setRenameOpen(false) })}
          isPending={renameSession.isPending}
        />
      )}
    </div>
  )
}
