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
  onNew: () => void
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
  onDelete: (id: string) => void
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
  onNew,
  onOpen,
  onRename,
  onClose,
  onDelete,
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

  if (sessions.length === 0) {
    return <EmptyState onNew={onNew} />
  }

  const accountMap = new Map(accounts.map((a) => [a.name, a]))

  const renderCard = (session: Session) => (
    <SessionCard
      key={session.id}
      session={session}
      account={accountMap.get(session.accountName)}
      selected={session.id === selectedId}
      onOpen={onOpen}
      onRename={onRename}
      onClose={onClose}
      onDelete={onDelete}
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
        ? groups.map((group) => (
            <div key={group.key} className="space-y-2">
              {group.label !== null && (
                <div
                  role="presentation"
                  aria-hidden="true"
                  className={cn(
                    'sticky top-0 z-10 flex items-center gap-2 px-1 py-1',
                    'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
                    'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
                  )}
                >
                  <span>{group.label}</span>
                  <span className="tabular-nums text-gray-400 dark:text-gray-500">{group.count}</span>
                </div>
              )}
              {group.sessions.map(renderCard)}
            </div>
          ))
        : sessions.map(renderCard)}
    </div>
  )
}
