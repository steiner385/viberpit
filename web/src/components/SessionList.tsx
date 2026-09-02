import { useState, useRef } from 'react'
import { Archive } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAutomations, useFixPr, useGenerateSuggestions, useSetAutopilotPaused, useBulkArchiveSessions } from '@/api/hooks'
import { SessionCard } from './SessionCard'
import { GroupedList } from './GroupedList'
import { EmptyState } from './EmptyState'
import { Button } from '@/ui/Button'
import type { Session, Account } from '@/api/types'
import type { SessionGroup } from '@/session/ordering'

// Sessions in these statuses are safe to bulk-archive via the quick action.
const ARCHIVABLE_STATUSES = new Set(['done', 'complete', 'ended'])

const CHECKBOX_CLS = cn(
  'h-4 w-4 flex-shrink-0 rounded border-gray-300 text-blue-600',
  'focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700',
  'dark:checked:bg-blue-500 cursor-pointer',
)

interface BulkActionBarProps {
  count: number
  archivableDoneCount: number
  archivePending: boolean
  onArchive: () => void
  onArchiveDone: () => void
  onClear: () => void
}

function BulkActionBar({ count, archivableDoneCount, archivePending, onArchive, onArchiveDone, onClear }: BulkActionBarProps) {
  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
        {count > 0 ? `${count} selected` : 'Select sessions to archive'}
      </span>
      <div className="flex items-center gap-2 ml-auto flex-wrap">
        {archivableDoneCount > 0 && (
          <Button
            type="button"
            variant="secondary"
            onClick={onArchiveDone}
            disabled={archivePending}
            className="min-h-[44px] text-xs"
          >
            <Archive size={14} className="mr-1" aria-hidden="true" />
            Archive done ({archivableDoneCount})
          </Button>
        )}
        {count > 0 && (
          <Button
            type="button"
            onClick={onArchive}
            disabled={archivePending}
            className={cn(
              'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
              'dark:bg-blue-700 dark:hover:bg-blue-600',
            )}
          >
            {archivePending ? 'Archiving…' : `Archive (${count})`}
          </Button>
        )}
        <Button type="button" variant="secondary" onClick={onClear} className="min-h-[44px]">
          Done
        </Button>
      </div>
    </div>
  )
}

interface SessionListProps {
  sessions: Session[]
  /** When provided, render grouped with section headers; else render flat `sessions`. */
  groups?: SessionGroup[]
  accounts: Account[]
  isLoading?: boolean
  selectedId?: string | null
  isFiltered?: boolean
  onClearSearch?: () => void
  onNew: () => void
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
  onDelete: (id: string) => void
  onQuickResume: (id: string, text?: string) => void
  /** Keys of groups currently collapsed. Omit for all-expanded. */
  collapsedKeys?: Set<string>
  /** Toggle a group's collapsed state by its key. */
  onToggleCollapse?: (key: string) => void
  selectedGroupKey?: string | null
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  listMode?: boolean
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
}

function SkeletonCard() {
  return (
    <div
      className={cn(
        'rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 p-3 min-h-[44px] animate-pulse',
      )}
      aria-hidden="true"
    >
      <div className="flex items-center gap-3">
        <div className="flex-1 space-y-2">
          <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/3" />
          <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded w-1/2" />
        </div>
        <div className="h-8 w-8 bg-gray-200 dark:bg-gray-700 rounded" />
      </div>
    </div>
  )
}

export function SessionList({
  sessions,
  groups,
  accounts,
  isLoading,
  selectedId,
  isFiltered,
  onClearSearch,
  onNew,
  onOpen,
  onRename,
  onClose,
  onDelete,
  onQuickResume,
  collapsedKeys,
  onToggleCollapse,
  selectedGroupKey,
  onExpand,
  onCollapse,
  listMode,
  onNavigate,
  onExit,
}: SessionListProps) {
  const { data: automations } = useAutomations()
  const prDoctorMode = automations?.find((a) => a.id === 'pr-doctor')?.mode ?? undefined
  const fixPr = useFixPr()
  const genSuggestions = useGenerateSuggestions()
  const setAutopilot = useSetAutopilotPaused()
  const bulkArchive = useBulkArchiveSessions()

  const [selectMode, setSelectMode] = useState(false)
  const [bulkSelected, setBulkSelected] = useState<Set<string>>(new Set())
  const [anchorId, setAnchorId] = useState<string | null>(null)
  const shiftHeld = useRef(false)

  // Drop stale ids when the session list changes (sessions archived or deleted elsewhere).
  if (bulkSelected.size > 0) {
    const present = new Set(sessions.map((s) => s.id))
    let changed = false
    const pruned = new Set<string>()
    for (const id of bulkSelected) {
      if (present.has(id)) pruned.add(id)
      else changed = true
    }
    if (changed) setBulkSelected(pruned)
  }

  function toggleOne(id: string, shift = false) {
    const ids = sessions.map((s) => s.id)
    const anchorIdx = anchorId === null ? -1 : ids.indexOf(anchorId)

    if (shift && anchorIdx !== -1) {
      const end = ids.indexOf(id)
      const [lo, hi] = anchorIdx < end ? [anchorIdx, end] : [end, anchorIdx]
      setBulkSelected((prev) => {
        const next = new Set(prev)
        for (let i = lo; i <= hi; i++) next.add(ids[i])
        return next
      })
      return
    }

    setBulkSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setAnchorId(id)
  }

  function clearSelection() {
    setBulkSelected(new Set())
    setSelectMode(false)
    setAnchorId(null)
  }

  function handleBulkArchive() {
    const ids = [...bulkSelected]
    if (ids.length === 0) return
    bulkArchive.mutate(ids, {
      onSuccess: () => clearSelection(),
    })
  }

  function handleArchiveDone() {
    const ids = sessions.filter((s) => ARCHIVABLE_STATUSES.has(s.status)).map((s) => s.id)
    if (ids.length === 0) return
    bulkArchive.mutate(ids, {
      onSuccess: () => clearSelection(),
    })
  }

  const archivableDoneCount = sessions.filter((s) => ARCHIVABLE_STATUSES.has(s.status)).length

  if (isLoading) {
    return (
      <div className="space-y-2" role="status" aria-label="Loading sessions" aria-busy="true">
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
      </div>
    )
  }

  // "Nothing to show" must key off `groups` when grouping is active, not the
  // flattened `sessions` prop. The caller feeds `sessions={orderedSessions}`,
  // which excludes collapsed groups — so collapsing every group empties
  // `sessions` while `groups` is still full. Keying the empty state off
  // `sessions` there would swallow the headers and read as "all sessions gone".
  const isEmpty = groups ? groups.length === 0 : sessions.length === 0
  if (isEmpty) {
    if (isFiltered) {
      return (
        <div
          className="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 p-6 text-center"
          role="status"
        >
          <p className="text-sm text-gray-500 dark:text-gray-400">No sessions match your search.</p>
          {onClearSearch && (
            <button
              type="button"
              onClick={onClearSearch}
              className="mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
            >
              Clear search
            </button>
          )}
        </div>
      )
    }
    return <EmptyState onNew={onNew} />
  }

  const accountMap = new Map(accounts.map((a) => [a.name, a]))

  const renderCard = (session: Session) => {
    const card = (
      <SessionCard
        session={session}
        account={accountMap.get(session.accountName)}
        selected={session.id === selectedId}
        listMode={listMode}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
        onQuickResume={onQuickResume}
        onGenerateSuggestion={(id) => genSuggestions.mutateAsync({ id })}
        onNavigate={onNavigate}
        onExit={onExit}
        prAutofixMode={prDoctorMode}
        onFixPr={(id, number) => fixPr.mutate({ id, number })}
        onToggleAutopilot={(id, paused) => setAutopilot.mutate({ id, paused })}
      />
    )

    if (!selectMode) return <div key={session.id}>{card}</div>

    return (
      <div key={session.id} className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={bulkSelected.has(session.id)}
          onClick={(e) => { shiftHeld.current = e.shiftKey }}
          onChange={() => toggleOne(session.id, shiftHeld.current)}
          aria-label={`Select ${session.name}`}
          className={CHECKBOX_CLS}
        />
        <div className="flex-1 min-w-0">{card}</div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {/* Bulk-select toolbar */}
      <div className="flex items-center justify-end gap-2">
        {!selectMode && archivableDoneCount > 0 && (
          <button
            type="button"
            onClick={handleArchiveDone}
            disabled={bulkArchive.isPending}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          >
            <Archive size={12} aria-hidden="true" />
            Archive done ({archivableDoneCount})
          </button>
        )}
        <button
          type="button"
          onClick={() => { setSelectMode((m) => !m); if (selectMode) clearSelection() }}
          className="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          aria-pressed={selectMode}
        >
          {selectMode ? 'Cancel' : 'Select'}
        </button>
      </div>

      {selectMode && (
        <BulkActionBar
          count={bulkSelected.size}
          archivableDoneCount={archivableDoneCount}
          archivePending={bulkArchive.isPending}
          onArchive={handleBulkArchive}
          onArchiveDone={handleArchiveDone}
          onClear={clearSelection}
        />
      )}

      {/* id and aria attrs mirror the original session-listbox contract for DispatchBar. */}
      <div
        id="session-listbox"
        aria-label="Sessions"
        aria-activedescendant={
          selectedId ? `row-${selectedId}` : selectedGroupKey ? `group-header-${selectedGroupKey}` : undefined
        }
        className="space-y-2"
      >
        {groups ? (
          <GroupedList
            groups={groups}
            renderRow={renderCard}
            collapsedKeys={collapsedKeys}
            onToggleCollapse={onToggleCollapse}
            selectedGroupKey={selectedGroupKey}
            onExpand={onExpand}
            onCollapse={onCollapse}
            listMode={listMode}
            onNavigate={onNavigate}
            onExit={onExit}
          />
        ) : (
          sessions.map(renderCard)
        )}
      </div>
    </div>
  )
}
