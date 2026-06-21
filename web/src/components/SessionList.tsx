import { cn } from '@/lib/utils'
import { SessionCard } from './SessionCard'
import { EmptyState } from './EmptyState'
import type { Session, Account } from '@/api/types'

interface SessionListProps {
  sessions: Session[]
  accounts: Account[]
  isLoading?: boolean
  selectedId?: string | null
  onNew: () => void
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
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
  accounts,
  isLoading,
  selectedId,
  onNew,
  onOpen,
  onRename,
  onClose,
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
      {sessions.map((session) => (
        <SessionCard
          key={session.id}
          session={session}
          account={accountMap.get(session.accountName)}
          selected={session.id === selectedId}
          onOpen={onOpen}
          onRename={onRename}
          onClose={onClose}
        />
      ))}
    </div>
  )
}
