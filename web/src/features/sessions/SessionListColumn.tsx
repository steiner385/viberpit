// web/src/features/sessions/SessionListColumn.tsx
//
// `app.list` — the Sessions list column, and the ONLY one.
//
// Dashboard (phone) and DesktopShell (wide) each carried their own copy of this
// markup, and the copies had drifted in four ways. The union kept here:
//
//   · PendingApprovalSection — phone-only before; desktop simply never showed
//     sessions parked at the launch-time model gate.
//   · DaemonHealthIndicator — phone-only before.
//   · the held-out set is `isPreSpawnStatus` (DEC-3), not desktop's narrower
//     `status === 'queued'`, which left `pending_approval` rows in the grouped
//     list with no tier mapping and no Approve/Deny controls.
//   · the selection cursor is `selectedId ?? nav.highlightedSession?.id`
//     (desktop's form): the open session wins, the keyboard highlight is the
//     fallback, so ↑/↓ still moves visibly on the new-session view.
//
// Layout is deliberately region-neutral — no width, no border, no landmark, and
// (since Task 10) not the `data-region` marker either: `shell/regions/ContextList`
// is the region, decides where `app.list` sits, how wide it is and what names it.
// This component fills what it is given, and only its middle band scrolls.
//
// There is no `listScrollTop`/`restoreListScroll` callback ref here. Desktop
// needed one because the whole shell sat inside a `SessionRuntimeProvider` keyed
// on the focused session id, so every click remounted the list and reset its
// scrollTop. With the provider scoped to the session pane, the list never
// remounts and the position survives on its own.
import { Plus } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SessionListControls } from '@/components/SessionListControls'
import { PendingApprovalSection } from '@/components/PendingApprovalSection'
import { QueuedSection } from '@/components/QueuedSection'
import { SessionList } from '@/components/SessionList'
import { AdoptPanel } from '@/components/AdoptPanel'
import { DaemonHealthIndicator } from '@/components/DaemonHealthIndicator'
import { NO_FILTERS, isFiltered as hasActiveFilters } from '@/session/listFilters'
import type { Account } from '@/api/types'
import type { SessionListState } from './useSessionListState'

export interface SessionListColumnProps {
  /** The list's whole state, from `useSessionListState()`. The shell holds the
   *  same handle so its prompt bar drives one cursor, not a second copy. */
  state: SessionListState
  accounts: Account[]
  isLoading?: boolean
  /** The open session, or null when the New Session slot is the target. */
  selectedId: string | null
  /** True while the New Session slot is the target — marks the button. */
  newSelected?: boolean
  /** `config.adoptionEnabled` — mounts AdoptPanel at the foot of the list. */
  adoptionEnabled?: boolean
  /** List-focus mode: ↑/↓ move inside the list instead of the prompt. */
  listMode?: boolean
  onNew: () => void
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
  onDelete: (id: string) => void
  onQuickResume: (id: string, text?: string) => void
  /** Leave list-focus mode (returns focus to the prompt). */
  onExitList?: () => void
}

export function SessionListColumn({
  state,
  accounts,
  isLoading,
  selectedId,
  newSelected = false,
  adoptionEnabled = false,
  listMode = false,
  onNew,
  onOpen,
  onRename,
  onClose,
  onDelete,
  onQuickResume,
  onExitList,
}: SessionListColumnProps) {
  const { sessions, queued, pendingApproval, search, ordering, nav } = state
  const { filters, setFilters, filterOptions } = state
  // One source of truth for "is anything narrowing the list" — `listFilters`'
  // own predicate, never a re-derived `status !== null || …` here.
  const filtered = hasActiveFilters(filters)

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Fixed head: the list controls stay above the scroll band. `empty:hidden`
          because the controls are conditional and they are now the band's only
          child — without it a session-less list paints a bare 1rem strip above
          the scroll area, which reads as a control that failed to render. */}
      <div className="shrink-0 space-y-2 p-2 empty:hidden">
        {/* Gated on the whole list, pre-spawn rows included: a lone queued
            session is still a session, and an empty toolbar reads as broken. */}
        {sessions.length > 0 && (
          <SessionListControls
            groupBy={ordering.groupBy}
            sortBy={ordering.sortBy}
            onGroupByChange={ordering.setGroupBy}
            onSortByChange={ordering.setSortBy}
            searchQuery={search.query}
            onSearchChange={search.setQuery}
            filters={{ value: filters, options: filterOptions, onChange: setFilters }}
          />
        )}
      </div>

      {/* The only scrolling band. Task 18 re-pins the scroll-preservation
          assertions against this testid — keep it. */}
      <div
        data-testid="session-list-scroll"
        className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2"
      >
        {/* Deliberately NOT hidden while a filter is active (the rendering half
            of the decision `useSessionListState` leaves open). A pre-spawn row
            has no tier and no host, so two of the three filters cannot describe
            it at all — hiding these sections would mean an unrelated selection
            silently swallowing an approval gate, which is blocking work someone
            is waiting on. Both sections are separately headed and counted, so a
            reader can see they are outside the filtered list. */}
        <PendingApprovalSection sessions={pendingApproval} onOpen={onOpen} />
        <QueuedSection sessions={queued} onOpen={onOpen} />
        {/* A filter can empty the list just as a search can; without this the
            zero-match case falls through to EmptyState's "create your first
            session", which is a lie about why the list is empty. The reset
            clears whatever caused it — clearing only the search would leave the
            button inert. The labelled "Clear filters" control in the strip
            above is the primary path; this is the in-place one. */}
        <SessionList
          sessions={ordering.orderedSessions}
          groups={ordering.groups}
          accounts={accounts}
          isLoading={isLoading}
          selectedId={selectedId ?? nav.highlightedSession?.id}
          selectedGroupKey={nav.highlightedGroupKey ?? null}
          onOpen={onOpen}
          onRename={onRename}
          onClose={onClose}
          onDelete={onDelete}
          isFiltered={search.isFiltered || filtered}
          onClearSearch={() => {
            search.setQuery('')
            if (filtered) setFilters(NO_FILTERS)
          }}
          onQuickResume={onQuickResume}
          listMode={listMode}
          onNavigate={nav.navigate}
          onExit={onExitList}
          collapsedKeys={ordering.collapsedKeys}
          onToggleCollapse={ordering.toggleCollapse}
          onExpand={ordering.expandGroup}
          onCollapse={ordering.collapseGroup}
        />
        {adoptionEnabled && <AdoptPanel />}
      </div>

      {/* Daemon health: collapses to nothing when there is no daemon to watch. */}
      <div className="shrink-0 px-2 empty:hidden">
        <DaemonHealthIndicator />
      </div>

      {/* Foot: New Session, deliberately the LAST thing in the column.

          It sits here rather than at the head so it is adjacent to `app.dock`,
          which is the surface it operates: the button's whole job is to clear
          the open session (`onNew` → navigate to `/sessions`), which flips the
          dock from `SessionComposer` to `DispatchBar`. Head-mounted, it was a
          control at one end of the column acting on a bar at the other; here,
          the bar it lights up is the next thing you look at. On phone and tablet
          the dock stacks directly beneath this button; on desktop the dock is in
          `app.main` and shares this bottom band across the resizer.

          Below `DaemonHealthIndicator`, not above it: both are `shrink-0` at the
          end of a flex column, so with the button last it holds the bottom edge
          whether or not the health line is showing. Above it, the button would
          shift by a row every time the daemon readout appeared or collapsed.

          It keeps its border and the column's own padding so that on phone,
          where it lands flush against the DispatchBar's surface, it still reads
          as a separate control rather than part of the bar. */}
      <div className="shrink-0 p-2">
        <button
          type="button"
          data-selected={newSelected}
          onClick={onNew}
          className={cn(
            'flex w-full min-h-target items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            newSelected
              ? 'border-accent bg-accent-wash text-accent'
              : 'border-edge text-ink hover:bg-surface-raised',
          )}
        >
          <Plus size={16} aria-hidden="true" />
          New Session
        </button>
      </div>
    </div>
  )
}
