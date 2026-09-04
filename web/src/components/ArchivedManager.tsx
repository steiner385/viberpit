/**
 * ArchivedManager — self-contained archived-sessions panel: the archived list
 * (select / review / unarchive / permanent-delete), a bulk action bar for
 * multi-select operations, and the permanent-delete dialog (single or bulk).
 * Owns its own data hooks; `onReviewSession` is called when the user opens a
 * read-only transcript review.
 *
 * Includes grouping / sorting / search via SessionListControls + GroupedList.
 */

import { useState, useRef, useEffect } from 'react'
import { Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import {
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
  useBulkUnarchiveSessions,
  useBulkPermanentDeleteSessions,
} from '@/api/hooks'
import type { BulkDeleteProgress } from '@/api/hooks'
import type { ArchivedSession, BatchResult } from '@/api/types'
import { useSessionSearch } from '@/session/useSessionSearch'
import { useItemOrdering } from '@/session/useSessionOrdering'
import {
  ARCHIVED_SPEC,
  ARCHIVED_GROUP_OPTIONS,
  ARCHIVED_SORT_OPTIONS,
} from '@/session/archivedOrdering'
import { SessionListControls } from '@/components/SessionListControls'
import { GroupedList } from '@/components/GroupedList'
import { prefersReducedMotion } from './session/brailleSpinner'

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
  progress?: BulkDeleteProgress | null   // bulk only: drives the progress bar
  onCancel?: () => void                  // bulk only: abort mid-run
}

function PermanentDeleteDialog({ names, onClose, onConfirm, isPending, progress, onCancel }: PermanentDeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const count = names.length
  const title = count === 1 ? 'Permanently delete session' : `Permanently delete ${count} sessions`

  return (
    <Dialog open onClose={onClose} title={title} size="sm" initialFocus={cancelRef} dismissable={!isPending}>
      {isPending && progress ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm text-ink-muted">
            <span>Deleting…</span>
            <span className="tabular-nums">{progress.done} of {progress.total}</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.done}
            aria-valuetext={`Deleting ${progress.done} of ${progress.total}`}
            className="h-2 w-full overflow-hidden rounded bg-surface-raised"
          >
            <div
              className={cn('h-full rounded bg-danger-strong', !prefersReducedMotion() && 'transition-[width] duration-200 ease-linear')}
              style={{ width: `${progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0}%` }}
            />
          </div>
        </div>
      ) : (
        <p className="text-sm text-ink-muted">
          {count === 1 ? (
            <>
              Permanently delete <span className="font-medium text-ink">&lsquo;{names[0]}&rsquo;</span>? This removes the
              session folder and all its data. This can&rsquo;t be undone.
            </>
          ) : (
            <>
              Permanently delete <span className="font-medium text-ink">{count} sessions</span>? This removes their
              session folders and all their data. This can&rsquo;t be undone.
            </>
          )}
        </p>
      )}

      <div className="flex gap-3 justify-end pt-2">
        <Button
          ref={cancelRef}
          type="button"
          variant="secondary"
          onClick={isPending ? onCancel : onClose}
          disabled={isPending && !onCancel}
        >
          Cancel
        </Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete permanently'}
        </Button>
      </div>
    </Dialog>
  )
}

// ── Quick Purge Bar ─────────────────────────────────────────────────────────────

// Shortcut counts for one-click purge of the oldest archived sessions.
const QUICK_PURGE_COUNTS = [25, 50, 100, 200] as const

interface QuickPurgeBarProps {
  total: number                 // how many archived sessions exist
  onPurge: (n: number) => void
  disabled: boolean             // a bulk delete is already running
}

function QuickPurgeBar({ total, onPurge, disabled }: QuickPurgeBarProps) {
  return (
    <div
      role="region"
      aria-label="Quick purge"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Purge oldest</span>
      <div className="flex flex-wrap items-center gap-2">
        {QUICK_PURGE_COUNTS.map((n) => (
          <Button
            key={n}
            type="button"
            variant="secondary"
            onClick={() => onPurge(n)}
            disabled={disabled || total < n}
            title={
              total < n
                ? `Only ${total} archived session${total === 1 ? '' : 's'}`
                : `Permanently delete the ${n} oldest archived sessions`
            }
            aria-label={`Purge the ${n} oldest archived sessions`}
            className="min-h-[44px]"
          >
            {n}
          </Button>
        ))}
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
        <Button type="button" variant="secondary" onClick={onClear}>Clear</Button>
      </div>
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

  // Search + ordering
  const search = useSessionSearch('archived')
  const ordering = useItemOrdering(archivedSessions, ARCHIVED_SPEC, search.matchedIds)
  // visible = filtered + sorted + collapsed-aware (drives selection / shift-range:
  // you can only select what you can see).
  const visible = ordering.orderedSessions
  // Collapse-INDEPENDENT: are there any sessions after the search filter, ignoring
  // which groups are collapsed? Render + empty-state decisions MUST key off this,
  // never `visible` — collapsing every group empties `visible`, so gating the list
  // on it also hides the headers, leaving no way to expand again (recoverable only
  // by clearing localStorage). Mirrors SessionList's `groups.length` gate.
  const hasFilteredSessions = ordering.groups.length > 0

  const [permDeleteTarget, setPermDeleteTarget] = useState<ArchivedSession | null>(null)
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // Last row toggled without shift — the pivot for shift-click range selection.
  const [anchorId, setAnchorId] = useState<string | null>(null)
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [bulkSnapshot, setBulkSnapshot] = useState<{ ids: string[]; names: string[] } | null>(null)
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

  function toggleOne(id: string, shiftKey = false) {
    // Range over the VISIBLE (grouped/sorted/filtered) order — a shift-click range
    // must follow what the user sees, not the raw archived list. A click whose anchor
    // is currently hidden (anchorIdx === -1) falls through to a plain toggle.
    const ids = visible.map((s) => s.id)
    const anchorIdx = anchorId === null ? -1 : ids.indexOf(anchorId)

    if (shiftKey && anchorIdx !== -1) {
      // Shift-click: select the inclusive range from the anchor to this row.
      const end = ids.indexOf(id)
      const [lo, hi] = anchorIdx < end ? [anchorIdx, end] : [end, anchorIdx]
      setSelected((prev) => {
        const next = new Set(prev)
        for (let i = lo; i <= hi; i++) next.add(ids[i])
        return next
      })
      // Anchor stays put so the range can be re-extended in either direction.
      return
    }

    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setAnchorId(id)
  }

  // toggleAll operates on the VISIBLE (filtered) set, not the raw list
  function toggleAll() {
    setSelected((prev) => {
      const visibleIds = visible.map((s) => s.id)
      const everySelected = visibleIds.length > 0 && visibleIds.every((id) => prev.has(id))
      return everySelected ? new Set() : new Set(visibleIds)
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

  function openBulkDelete() {
    const sel = archivedSessions.filter((s) => selected.has(s.id))
    if (sel.length === 0) return
    setBulkSnapshot({ ids: sel.map((s) => s.id), names: sel.map(displayNameOf) })
    setBulkDeleteOpen(true)
  }

  // Quick-purge the N oldest archived sessions. Reuses the bulk-delete confirm dialog
  // (progress + cancel come for free). archivedAt is an ISO string, so a lexical sort
  // is chronological — oldest first.
  function openQuickPurge(n: number) {
    const oldest = [...archivedSessions]
      .sort((a, b) => a.archivedAt.localeCompare(b.archivedAt))
      .slice(0, n)
    if (oldest.length === 0) return
    setBulkSnapshot({ ids: oldest.map((s) => s.id), names: oldest.map(displayNameOf) })
    setBulkDeleteOpen(true)
  }

  function confirmBulkDelete() {
    const ids = bulkSnapshot?.ids ?? []
    if (ids.length === 0) { setBulkDeleteOpen(false); setBulkSnapshot(null); return }
    bulkDelete.mutate(ids, {
      onSuccess: (res) => {
        setSummary(summarize('Deleted', res))
        setSelected(new Set())
        setBulkDeleteOpen(false)
        setBulkSnapshot(null)
      },
      onError: () => { setBulkDeleteOpen(false); setBulkSnapshot(null) },
    })
  }

  // ── Per-row renderer ──────────────────────────────────────────────────────────
  // Returns a keyed <div> (not <li>) because GroupedList panels are <div role="group">
  // and bare <li> elements inside a <div> are invalid HTML.
  function renderArchivedRow(s: ArchivedSession) {
    const displayName = displayNameOf(s)
    const archivedDate = new Date(s.archivedAt).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
    return (
      <div
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
              onClick={(e) => { shiftHeld.current = e.shiftKey }}
              onChange={() => toggleOne(s.id, shiftHeld.current)}
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
              onClick={() => onReviewSession(s.id)}
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
              onClick={() => handleUnarchive(s.id)}
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
              onClick={() => handlePermanentDelete(s)}
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
      </div>
    )
  }

  // ── Select-all header state — operates on the VISIBLE (filtered) set ──────────
  const selectAllRef = useRef<HTMLInputElement>(null)
  // A checkbox's `change` event doesn't carry modifier keys, but its `click` does — and click
  // always fires first (even for keyboard/space toggles). Capture shift here, read it in onChange.
  const shiftHeld = useRef(false)
  const allSelected = visible.length > 0 && visible.every((s) => selected.has(s.id))
  const someSelected = visible.some((s) => selected.has(s.id))

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected && !allSelected
  }, [someSelected, allSelected])

  return (
    <div className="space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Archived Sessions
      </h2>

      {summary && (
        <p role="status" className="text-xs text-gray-500 dark:text-gray-400">{summary}</p>
      )}

      {archivedSessions.length > 0 && (
        <QuickPurgeBar
          total={archivedSessions.length}
          onPurge={openQuickPurge}
          disabled={bulkDelete.isPending}
        />
      )}

      {selected.size > 0 && (
        <BulkActionBar
          count={selected.size}
          onUnarchive={handleBulkUnarchive}
          onDelete={openBulkDelete}
          onClear={clearSelection}
          unarchivePending={bulkUnarchive.isPending}
        />
      )}

      {/* Controls strip — only when there are archived sessions at all */}
      {archivedSessions.length > 0 && (
        <SessionListControls
          groupBy={ordering.groupBy}
          sortBy={ordering.sortBy}
          onGroupByChange={ordering.setGroupBy}
          onSortByChange={ordering.setSortBy}
          groupOptions={ARCHIVED_GROUP_OPTIONS}
          sortOptions={ARCHIVED_SORT_OPTIONS}
          searchQuery={search.query}
          onSearchChange={search.setQuery}
          searchPlaceholder="Search archived…"
          searchAriaLabel="Search archived sessions"
        />
      )}

      {/* Empty state: no archived sessions at all */}
      {archivedSessions.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
          No archived sessions.
        </p>
      )}

      {/* Empty state: search active but no matches */}
      {archivedSessions.length > 0 && search.isFiltered && !hasFilteredSessions && (
        <div className="py-6 text-center space-y-2">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            No archived sessions match your search.
          </p>
          <button
            type="button"
            onClick={() => search.setQuery('')}
            className={cn(
              'text-xs text-blue-600 dark:text-blue-400 underline',
              'hover:text-blue-700 dark:hover:text-blue-300',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
            )}
          >
            Clear
          </button>
        </div>
      )}

      {/* List with grouping — render whenever any session survives the search
          filter; collapsed groups stay in the list (as headers) so they can be
          re-expanded, so this must NOT gate on the collapse-aware `visible`. */}
      {archivedSessions.length > 0 && hasFilteredSessions && (
        <div className="space-y-2">
          {/* Select-all header row */}
          <div className="flex items-center gap-2 px-1">
            <input
              ref={selectAllRef}
              type="checkbox"
              checked={allSelected}
              onChange={toggleAll}
              aria-label="Select all"
              className={CHECKBOX_CLS}
            />
            <span className="text-xs text-gray-500 dark:text-gray-400">Select all</span>
          </div>

          <div role="list" aria-label="Archived sessions">
            <GroupedList
              groups={ordering.groups}
              renderRow={renderArchivedRow}
              collapsedKeys={ordering.collapsedKeys}
              onToggleCollapse={ordering.toggleCollapse}
            />
          </div>
        </div>
      )}

      {permDeleteTarget && (
        <PermanentDeleteDialog
          names={[displayNameOf(permDeleteTarget)]}
          onClose={() => setPermDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}

      {bulkDeleteOpen && bulkSnapshot && (
        <PermanentDeleteDialog
          names={bulkSnapshot.names}
          onClose={() => { setBulkDeleteOpen(false); setBulkSnapshot(null) }}
          onConfirm={confirmBulkDelete}
          isPending={bulkDelete.isPending}
          progress={bulkDelete.progress}
          onCancel={bulkDelete.cancel}
        />
      )}
    </div>
  )
}
