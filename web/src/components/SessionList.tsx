import { cn } from '@/lib/utils'
import { useAutomations, useFixPr, useGenerateSuggestions } from '@/api/hooks'
import { SessionCard } from './SessionCard'
import { GroupedList } from './GroupedList'
import { EmptyState } from './EmptyState'
import type { Session, Account } from '@/api/types'
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

  const renderCard = (session: Session) => (
    <SessionCard
      key={session.id}
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
    />
  )

  return (
    // id="session-listbox" is referenced by DispatchBar's aria-controls.
    // aria-activedescendant on the container tells assistive tech which row is
    // "virtually focused" while real keyboard focus stays in the textarea below.
    // We deliberately do NOT set role="listbox" here because the rows contain
    // interactive buttons (overflow menus) which would violate the aria-required-children
    // and nested-interactive ARIA rules for listbox/option.
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
  )
}
