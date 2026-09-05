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
import { Button } from '@/ui/Button'
import {
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
  useBulkUnarchiveSessions,
  useBulkPermanentDeleteSessions,
} from '@/api/hooks'
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
import { PermanentDeleteDialog } from './PermanentDeleteDialog'
import { BulkActionBar } from './list/BulkActionBar'
import { NoMatchState } from './list/NoMatchState'
import { SelectableRow, SELECT_CHECKBOX_CLS } from './list/SelectableRow'
import { useBulkSelection } from './list/useBulkSelection'
import { displayNameOf } from './ArchivedSessionCard'

function summarize(verb: string, res: BatchResult): string {
  const ok = res.succeeded.length
  const bad = res.failed.length
  return bad > 0 ? `${verb} ${ok} · ${bad} failed` : `${verb} ${ok}`
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
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [bulkSnapshot, setBulkSnapshot] = useState<{ ids: string[]; names: string[] } | null>(null)
  const [summary, setSummary] = useState<string | null>(null)

  // Range + select-all follow the VISIBLE order; a selection survives in the
  // RAW list, so collapsing a group or typing in the search box does not
  // silently drop rows the user has already ticked.
  const selection = useBulkSelection(
    visible.map((s) => s.id),
    archivedSessions.map((s) => s.id),
  )

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
    const ids = [...selection.selected]
    if (ids.length === 0) return
    bulkUnarchive.mutate(ids, {
      onSuccess: (res) => { setSummary(summarize('Unarchived', res)); selection.clear() },
    })
  }

  function openBulkDelete() {
    const sel = archivedSessions.filter((s) => selection.isSelected(s.id))
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
        selection.clear()
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
      <SelectableRow
        key={s.id}
        id={s.id}
        name={displayName}
        checked={selection.isSelected(s.id)}
        onToggle={selection.toggleOne}
      >
        <div
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
      </SelectableRow>
    )
  }

  // ── Select-all header state — operates on the VISIBLE (filtered) set ──────────
  const selectAllRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selection.someSelected && !selection.allSelected
    }
  }, [selection.someSelected, selection.allSelected])

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

      {selection.count > 0 && (
        <BulkActionBar label={`${selection.count} selected`} onClear={selection.clear}>
          <Button
            type="button"
            onClick={handleBulkUnarchive}
            disabled={bulkUnarchive.isPending}
            className={cn(
              'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
            )}
          >
            {bulkUnarchive.isPending ? 'Unarchiving…' : `Unarchive (${selection.count})`}
          </Button>
          <Button
            type="button"
            onClick={openBulkDelete}
            className={cn(
              'min-h-[44px] bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600 focus-visible:ring-red-500',
            )}
          >
            Delete ({selection.count})
          </Button>
        </BulkActionBar>
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
        <NoMatchState
          message="No archived sessions match your search."
          actionLabel="Clear"
          onClear={() => search.setQuery('')}
        />
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
              checked={selection.allSelected}
              onChange={selection.toggleAll}
              aria-label="Select all"
              className={SELECT_CHECKBOX_CLS}
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
