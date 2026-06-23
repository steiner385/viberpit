/**
 * ArchivedManager — self-contained archived-sessions panel: the archived list
 * (select / review / unarchive / permanent-delete), a bulk action bar for
 * multi-select operations, and the permanent-delete dialog (single or bulk).
 * Owns its own data hooks; `onReviewSession` is called when the user opens a
 * read-only transcript review.
 */

import * as React from 'react'
import { useState, useRef, useEffect } from 'react'
import { Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
  useBulkUnarchiveSessions,
  useBulkPermanentDeleteSessions,
} from '@/api/hooks'
import type { ArchivedSession, BatchResult } from '@/api/types'

const CHECKBOX_CLS = cn(
  'h-4 w-4 rounded border-gray-300 dark:border-gray-600',
  'text-blue-600 focus-visible:ring-2 focus-visible:ring-blue-500',
)

function displayNameOf(s: ArchivedSession): string {
  return s.name || s.id.slice(0, 8)
}

function summarize(verb: string, res: BatchResult): string {
  const ok = res.succeeded.length
  const bad = res.failed.length
  return bad > 0 ? `${verb} ${ok} · ${bad} failed` : `${verb} ${ok}`
}

// ── Permanent Delete Dialog (single or bulk) ───────────────────────────────────

interface PermanentDeleteDialogProps {
  names: string[]            // 1 = single delete, >1 = bulk delete
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function PermanentDeleteDialog({ names, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const count = names.length

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
          {count === 1 ? 'Permanently delete session' : `Permanently delete ${count} sessions`}
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          {count === 1 ? (
            <>
              Permanently delete <span className="font-medium">&lsquo;{names[0]}&rsquo;</span>? This removes the
              session folder and all its data. This can&rsquo;t be undone.
            </>
          ) : (
            <>
              Permanently delete <span className="font-medium">{count} sessions</span>? This removes their
              session folders and all their data. This can&rsquo;t be undone.
            </>
          )}
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

// ── Bulk Action Bar ─────────────────────────────────────────────────────────────

interface BulkActionBarProps {
  count: number
  onUnarchive: () => void
  onDelete: () => void
  onClear: () => void
  unarchivePending: boolean
}

function BulkActionBar({ count, onUnarchive, onDelete, onClear, unarchivePending }: BulkActionBarProps) {
  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{count} selected</span>
      <div className="flex items-center gap-2 ml-auto">
        <Button
          type="button"
          onClick={onUnarchive}
          disabled={unarchivePending}
          className={cn(
            'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
            'dark:bg-blue-700 dark:hover:bg-blue-600',
          )}
        >
          {unarchivePending ? 'Unarchiving…' : `Unarchive (${count})`}
        </Button>
        <Button
          type="button"
          onClick={onDelete}
          className={cn(
            'min-h-[44px] bg-red-600 hover:bg-red-700 text-white',
            'dark:bg-red-700 dark:hover:bg-red-600 focus-visible:ring-red-500',
          )}
        >
          Delete ({count})
        </Button>
        <Button type="button" variant="outline" onClick={onClear}>Clear</Button>
      </div>
    </div>
  )
}

// ── Archived List ──────────────────────────────────────────────────────────────

interface ArchivedListProps {
  sessions: ArchivedSession[]
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: () => void
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  onPermanentDelete: (session: ArchivedSession) => void
  unarchivingId: string | null
}

function ArchivedList({
  sessions, selected, onToggle, onToggleAll,
  onReview, onUnarchive, onPermanentDelete, unarchivingId,
}: ArchivedListProps) {
  const selectAllRef = useRef<HTMLInputElement>(null)
  const allSelected = sessions.length > 0 && sessions.every((s) => selected.has(s.id))
  const someSelected = sessions.some((s) => selected.has(s.id))

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected && !allSelected
  }, [someSelected, allSelected])

  if (sessions.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
        No archived sessions.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 px-1">
        <input
          ref={selectAllRef}
          type="checkbox"
          checked={allSelected}
          onChange={onToggleAll}
          aria-label="Select all"
          className={CHECKBOX_CLS}
        />
        <span className="text-xs text-gray-500 dark:text-gray-400">Select all</span>
      </div>

      <ul className="space-y-2" aria-label="Archived sessions">
        {sessions.map((s) => {
          const displayName = displayNameOf(s)
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
                <div className="flex items-start gap-3 flex-1 min-w-0">
                  <input
                    type="checkbox"
                    checked={selected.has(s.id)}
                    onChange={() => onToggle(s.id)}
                    aria-label={`Select ${displayName}`}
                    className={cn(CHECKBOX_CLS, 'mt-0.5')}
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{displayName}</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                      {s.account} · Archived {archivedDate}
                    </p>
                  </div>
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
    </div>
  )
}

// ── ArchivedManager ──────────────────────────────────────────────────────────

export function ArchivedManager({ onReviewSession }: { onReviewSession: (id: string) => void }) {
  const { data: archivedSessions = [] } = useArchivedSessions()
  const unarchiveSession = useUnarchiveSession()
  const permanentDeleteSession = usePermanentDeleteSession()
  const bulkUnarchive = useBulkUnarchiveSessions()
  const bulkDelete = useBulkPermanentDeleteSessions()

  const [permDeleteTarget, setPermDeleteTarget] = useState<ArchivedSession | null>(null)
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [summary, setSummary] = useState<string | null>(null)

  // Drop selected ids that no longer exist after a refetch.
  // Done at render time (not in an effect) to avoid the set-state-in-effect lint rule —
  // React re-renders synchronously when state set during render differs from current value.
  if (selected.size > 0) {
    const present = new Set(archivedSessions.map((s) => s.id))
    let changed = false
    const pruned = new Set<string>()
    for (const id of selected) {
      if (present.has(id)) pruned.add(id)
      else changed = true
    }
    if (changed) setSelected(pruned)
  }

  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleAll() {
    setSelected((prev) => {
      const allIds = archivedSessions.map((s) => s.id)
      const everySelected = allIds.length > 0 && allIds.every((id) => prev.has(id))
      return everySelected ? new Set() : new Set(allIds)
    })
  }

  function clearSelection() {
    setSelected(new Set())
  }

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

  function handleBulkUnarchive() {
    const ids = [...selected]
    if (ids.length === 0) return
    bulkUnarchive.mutate(ids, {
      onSuccess: (res) => { setSummary(summarize('Unarchived', res)); setSelected(new Set()) },
    })
  }

  function confirmBulkDelete() {
    const ids = [...selected]
    if (ids.length === 0) { setBulkDeleteOpen(false); return }
    bulkDelete.mutate(ids, {
      onSuccess: (res) => { setSummary(summarize('Deleted', res)); setSelected(new Set()); setBulkDeleteOpen(false) },
      onError: () => setBulkDeleteOpen(false),
    })
  }

  const selectedNames = archivedSessions.filter((s) => selected.has(s.id)).map(displayNameOf)

  return (
    <div className="space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Archived Sessions
      </h2>

      {summary && (
        <p role="status" className="text-xs text-gray-500 dark:text-gray-400">{summary}</p>
      )}

      {selected.size > 0 && (
        <BulkActionBar
          count={selected.size}
          onUnarchive={handleBulkUnarchive}
          onDelete={() => setBulkDeleteOpen(true)}
          onClear={clearSelection}
          unarchivePending={bulkUnarchive.isPending}
        />
      )}

      <ArchivedList
        sessions={archivedSessions}
        selected={selected}
        onToggle={toggleOne}
        onToggleAll={toggleAll}
        onReview={onReviewSession}
        onUnarchive={handleUnarchive}
        onPermanentDelete={handlePermanentDelete}
        unarchivingId={unarchivingId}
      />

      {permDeleteTarget && (
        <PermanentDeleteDialog
          names={[displayNameOf(permDeleteTarget)]}
          onClose={() => setPermDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}

      {bulkDeleteOpen && (
        <PermanentDeleteDialog
          names={selectedNames}
          onClose={() => setBulkDeleteOpen(false)}
          onConfirm={confirmBulkDelete}
          isPending={bulkDelete.isPending}
        />
      )}
    </div>
  )
}
