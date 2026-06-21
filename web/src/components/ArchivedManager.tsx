/**
 * ArchivedManager — self-contained archived-sessions panel: the archived list
 * (review / unarchive / permanent-delete) plus the permanent-delete dialog. Owns
 * its own data hooks; `onReviewSession` is called when the user opens a read-only
 * transcript review. Used by the Dashboard tab panel (mobile) and the desktop
 * main pane.
 */

import * as React from 'react'
import { useState, useRef } from 'react'
import { Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
} from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'

// ── Permanent Delete Dialog ───────────────────────────────────────────────────

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  React.useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perm-delete-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-sm rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
        )}
      >
        <h2
          id="perm-delete-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Permanently delete session
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          Permanently delete <span className="font-medium">&lsquo;{sessionName}&rsquo;</span>? This removes the
          session folder and all its data. This can&rsquo;t be undone.
        </p>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className={cn(
              'min-h-[44px]',
              'bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600',
              'focus-visible:ring-red-500',
            )}
          >
            {isPending ? 'Deleting…' : 'Delete permanently'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── Archived List ──────────────────────────────────────────────────────────────

interface ArchivedListProps {
  sessions: ArchivedSession[]
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  onPermanentDelete: (session: ArchivedSession) => void
  unarchivingId: string | null
}

function ArchivedList({ sessions, onReview, onUnarchive, onPermanentDelete, unarchivingId }: ArchivedListProps) {
  if (sessions.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
        No archived sessions.
      </p>
    )
  }

  return (
    <ul className="space-y-2" aria-label="Archived sessions">
      {sessions.map((s) => {
        const displayName = s.name || s.id.slice(0, 8)
        const archivedDate = new Date(s.archivedAt).toLocaleDateString(undefined, {
          year: 'numeric', month: 'short', day: 'numeric',
        })
        return (
          <li
            key={s.id}
            className={cn(
              'flex flex-col gap-2 rounded-lg border border-gray-200 dark:border-gray-700',
              'bg-white dark:bg-gray-800 px-3 py-2',
            )}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{displayName}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                  {s.account} · Archived {archivedDate}
                </p>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  type="button"
                  onClick={() => onReview(s.id)}
                  className={cn(
                    'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                    'border border-gray-300 dark:border-gray-600',
                    'text-gray-700 dark:text-gray-300',
                    'hover:bg-gray-100 dark:hover:bg-gray-700',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                  )}
                  aria-label={`Review ${displayName}`}
                >
                  Review
                </button>
                <button
                  type="button"
                  onClick={() => onUnarchive(s.id)}
                  disabled={unarchivingId === s.id}
                  className={cn(
                    'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                    'bg-blue-600 hover:bg-blue-700 text-white',
                    'dark:bg-blue-700 dark:hover:bg-blue-600',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                    'disabled:opacity-40',
                  )}
                  aria-label={`Unarchive ${displayName}`}
                >
                  {unarchivingId === s.id ? 'Restoring…' : 'Unarchive'}
                </button>
                <button
                  type="button"
                  onClick={() => onPermanentDelete(s)}
                  className={cn(
                    'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                    'text-red-500 dark:text-red-400',
                    'hover:bg-red-50 dark:hover:bg-red-900/30',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500',
                  )}
                  aria-label={`Delete ${displayName} permanently`}
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// ── ArchivedManager ──────────────────────────────────────────────────────────

export function ArchivedManager({ onReviewSession }: { onReviewSession: (id: string) => void }) {
  const { data: archivedSessions = [] } = useArchivedSessions()
  const unarchiveSession = useUnarchiveSession()
  const permanentDeleteSession = usePermanentDeleteSession()

  const [permDeleteTarget, setPermDeleteTarget] = useState<ArchivedSession | null>(null)
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null)

  function handleUnarchive(id: string) {
    setUnarchivingId(id)
    unarchiveSession.mutate(id, {
      onSettled: () => setUnarchivingId(null),
    })
  }

  function handlePermanentDelete(session: ArchivedSession) {
    setPermDeleteTarget(session)
  }

  function confirmPermanentDelete() {
    if (!permDeleteTarget) return
    permanentDeleteSession.mutate(permDeleteTarget.id, {
      onSuccess: () => setPermDeleteTarget(null),
      onError: () => setPermDeleteTarget(null),
    })
  }

  return (
    <div className="space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Archived Sessions
      </h2>
      <ArchivedList
        sessions={archivedSessions}
        onReview={onReviewSession}
        onUnarchive={handleUnarchive}
        onPermanentDelete={handlePermanentDelete}
        unarchivingId={unarchivingId}
      />

      {permDeleteTarget && (
        <PermanentDeleteDialog
          sessionName={permDeleteTarget.name || permDeleteTarget.id.slice(0, 8)}
          onClose={() => setPermDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}
    </div>
  )
}
