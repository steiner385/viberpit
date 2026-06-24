import { ChevronRight, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SessionCard } from './SessionCard'
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
  listMode,
  onNavigate,
  onExit,
}: SessionListProps) {
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
      onNavigate={onNavigate}
      onExit={onExit}
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
      aria-activedescendant={selectedId ? `row-${selectedId}` : undefined}
      className="space-y-2"
    >
      {groups
        ? groups.map((group) => {
            const collapsed = collapsedKeys?.has(group.key) ?? false
            const panelId = `group-panel-${group.key}`
            return (
              <div key={group.key} className="space-y-2">
                {group.label !== null && (
                  <button
                    type="button"
                    onClick={() => onToggleCollapse?.(group.key)}
                    aria-expanded={!collapsed}
                    aria-controls={collapsed ? undefined : panelId}
                    className={cn(
                      'sticky top-0 z-10 flex w-full items-center gap-2 px-1 py-1',
                      'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
                      'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
                      'hover:text-gray-700 dark:hover:text-gray-200',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
                    )}
                  >
                    {collapsed ? (
                      <ChevronRight size={14} aria-hidden />
                    ) : (
                      <ChevronDown size={14} aria-hidden />
                    )}
                    <span>{group.label}</span>
                    <span className="tabular-nums text-gray-400 dark:text-gray-500">{group.count}</span>
                  </button>
                )}
                {!collapsed && (
                  <div id={panelId} className="space-y-2">
                    {group.sessions.map(renderCard)}
                  </div>
                )}
              </div>
            )
          })
        : sessions.map(renderCard)}
    </div>
  )
}
