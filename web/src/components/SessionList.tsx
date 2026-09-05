import { useState } from 'react'
import { Archive, RotateCcw, Shuffle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAutomations, useFixPr, useGenerateSuggestions, useSetAutopilotPaused, useBulkArchiveSessions, useResumeInterrupted, useRotateStalled } from '@/api/hooks'
import { SessionCard } from './SessionCard'
import { GroupedList } from './GroupedList'
import { EmptyState } from './EmptyState'
import { BulkActionBar } from './list/BulkActionBar'
import { NoMatchState } from './list/NoMatchState'
import { SelectableRow } from './list/SelectableRow'
import { SkeletonCard } from './list/SkeletonCard'
import { useBulkSelection } from './list/useBulkSelection'
import { Button } from '@/ui/Button'
import { ARCHIVABLE_STATUSES, type Session, type Account } from '@/api/types'
import type { SessionGroup } from '@/session/ordering'

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
  const resumeInterrupted = useResumeInterrupted()
  const rotateStalled = useRotateStalled()

  const [selectMode, setSelectMode] = useState(false)
  // The prune list is the visible one: SessionList is handed `orderedSessions`,
  // which is already collapse-aware, and that is the set today's behaviour
  // prunes against.
  const selection = useBulkSelection(sessions.map((s) => s.id))

  function clearSelection() {
    selection.clear()
    setSelectMode(false)
  }

  function handleBulkArchive() {
    const ids = [...selection.selected]
    if (ids.length === 0) return
    bulkArchive.mutate(ids, { onSuccess: () => clearSelection() })
  }

  function handleArchiveDone() {
    const ids = sessions.filter((s) => ARCHIVABLE_STATUSES.has(s.status)).map((s) => s.id)
    if (ids.length === 0) return
    bulkArchive.mutate(ids, {
      onSuccess: () => clearSelection(),
    })
  }

  const archivableDoneCount = sessions.filter((s) => ARCHIVABLE_STATUSES.has(s.status)).length
  const resumableIds = sessions.filter((s) => s.resumable).map((s) => s.id)
  const handleResumeInterrupted = () => {
    if (resumableIds.length > 0) resumeInterrupted.mutate(resumableIds)
  }
  const stalledIds = sessions.filter((s) => s.usageStalled).map((s) => s.id)
  const handleRotateStalled = () => {
    if (stalledIds.length > 0) rotateStalled.mutate(stalledIds)
  }

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
      // Copy covers BOTH causes. Task 9 gave the list status/project/
      // environment filters, so "your search" became a lie for the filter
      // case — and the reset already clears whichever caused it
      // (`SessionListColumn` clears the query AND the filters), so the
      // wording was the only thing that was search-only.
      return (
        <NoMatchState
          message="No sessions match your search or filters."
          actionLabel="Clear search and filters"
          onClear={() => onClearSearch?.()}
        />
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
      <SelectableRow
        key={session.id}
        id={session.id}
        name={session.name}
        checked={selection.isSelected(session.id)}
        onToggle={selection.toggleOne}
      >
        {card}
      </SelectableRow>
    )
  }

  return (
    <div className="space-y-2">
      {/* Bulk-select toolbar */}
      <div className="flex items-center justify-end gap-2">
        {!selectMode && resumableIds.length > 0 && (
          <button
            type="button"
            onClick={handleResumeInterrupted}
            disabled={resumeInterrupted.isPending}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          >
            <RotateCcw size={12} aria-hidden="true" />
            Resume interrupted ({resumableIds.length})
          </button>
        )}
        {!selectMode && stalledIds.length > 0 && (
          <button
            type="button"
            onClick={handleRotateStalled}
            disabled={rotateStalled.isPending}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          >
            <Shuffle size={12} aria-hidden="true" />
            Rotate stalled ({stalledIds.length})
          </button>
        )}
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
          label={selection.count > 0 ? `${selection.count} selected` : 'Select sessions to archive'}
          clearLabel="Done"
          onClear={clearSelection}
        >
          {archivableDoneCount > 0 && (
            <Button
              type="button"
              variant="secondary"
              onClick={handleArchiveDone}
              disabled={bulkArchive.isPending}
              className="min-h-[44px] text-xs"
            >
              <Archive size={14} className="mr-1" aria-hidden="true" />
              Archive done ({archivableDoneCount})
            </Button>
          )}
          {selection.count > 0 && (
            <Button
              type="button"
              onClick={handleBulkArchive}
              disabled={bulkArchive.isPending}
              className={cn(
                'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
                'dark:bg-blue-700 dark:hover:bg-blue-600',
              )}
            >
              {bulkArchive.isPending ? 'Archiving…' : `Archive (${selection.count})`}
            </Button>
          )}
        </BulkActionBar>
      )}

      {/* The `aria-controls` target of the DispatchBar textarea — that textarea is
          also where `aria-activedescendant` lives, and the only place it is valid.
          A second copy used to sit here on this plain <div>: aria-activedescendant
          is not an allowed attribute on a role-less element (axe
          `aria-allowed-attr`), so assistive tech ignored it while it drifted from
          the real one. DispatchBar owns the cursor; this element owns the id. */}
      <div id="session-listbox" aria-label="Sessions" className="space-y-2">
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
