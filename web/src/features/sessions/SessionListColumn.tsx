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
// Since the scope switch it is BOTH lists: `ScopeSegment` in the fixed head,
// then either `LiveScopeBody` or `ArchivedScopeBody` — siblings, never nested
// branches, so neither scope's hooks run behind the other. Archived used to be
// a top-level nav page; it is a scope of this column now, derived from the route
// by `listScopeForPath` in the shell and passed down as `scope`.
//
// There is no `listScrollTop`/`restoreListScroll` callback ref here. Desktop
// needed one because the whole shell sat inside a `SessionRuntimeProvider` keyed
// on the focused session id, so every click remounted the list and reset its
// scrollTop. With the provider scoped to the session pane, the list never
// remounts and the position survives on its own.
import { Plus } from 'lucide-react'
import { useArchivedSessions } from '@/api/hooks'
import { SessionListControls } from '@/components/SessionListControls'
import { PendingApprovalSection } from '@/components/PendingApprovalSection'
import { QueuedSection } from '@/components/QueuedSection'
import { SessionList } from '@/components/SessionList'
import { AdoptPanel } from '@/components/AdoptPanel'
import { DaemonHealthIndicator } from '@/components/DaemonHealthIndicator'
import { NO_FILTERS, isFiltered as hasActiveFilters } from '@/session/listFilters'
import type { ListScope } from '@/session/sessionNavState'
import type { Account } from '@/api/types'
import { ArchivedScopeBody } from './ArchivedScopeBody'
import { ScopeBody } from './ScopeBody'
import { ScopeSegment } from './ScopeSegment'
import type { SessionListState } from './useSessionListState'

export interface SessionListColumnProps {
  /** Which scope the ROUTE is in (`listScopeForPath`, read once in the shell).
   *  The column never re-derives it — one derivation, passed down. */
  scope: ListScope
  /** The list's whole state, from `useSessionListState()`. The shell holds the
   *  same handle so its prompt bar drives one cursor, not a second copy.
   *  LIVE only: the archived scope's state is `useArchivedListState`, mounted
   *  inside `ArchivedScopeBody` so its poll runs only while it is on screen. */
  state: SessionListState
  accounts: Account[]
  isLoading?: boolean
  /** The open session, or null when the New Session slot is the target. */
  selectedId: string | null
  /** True while the New Session slot is the target. The dock is then already
   *  the DispatchBar, so the foot button is NOT rendered (see the foot). */
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
  /** Open an archived session's read-only transcript (archived scope only). */
  onReviewSession: (id: string) => void
  /** Leave list-focus mode (returns focus to the prompt). */
  onExitList?: () => void
}

/**
 * The column frame: fixed head (scope switch), one scope body, fixed foot.
 *
 * The two bodies are SIBLINGS rather than branches inside one body, so neither
 * scope's hooks run behind the other and no hook is called conditionally: the
 * live handle is a prop (the shell shares it with the prompt bar), the archived
 * one is `ArchivedScopeBody`'s own.
 */
export function SessionListColumn({
  scope,
  state,
  newSelected = false,
  onNew,
  onReviewSession,
  ...live
}: SessionListColumnProps) {
  // The archived COUNT, for the segment. This subscription is UNCONDITIONAL —
  // the count is on screen in both scopes, so the archived query (10 s stale /
  // 30 s poll) is live in both, exactly as it was when the nav badge read it.
  // Same query key as `ArchivedScopeBody`'s, so mounting that body adds a
  // subscriber and not a second fetch. What stays scope-local is the archived
  // SEARCH and ORDERING state, which belongs to the body that renders the rows.
  // The live poll is untouched either way: two queries, never merged (D4).
  const { data: archived = [] } = useArchivedSessions()

  return (
    // `overflow-hidden`: this column is a stack of `shrink-0` bands around one
    // scroll band, and at a short enough viewport (a phone with the keyboard up
    // — see the `short:` variant in index.css) the bands alone are taller than
    // the box the shell gives it. Without a clip the surplus paints OUTSIDE the
    // column, and `app.main` — an opaque later flex item, so it paints on top —
    // covers it: the visible bug was a stray sliver of whichever control
    // straddled the dock's top border. Nothing this hides was reachable anyway;
    // `short:hidden` on the controls strip is what actually buys back the room.
    // Safe for the row menus: they are `absolute` inside the scroll band, which
    // already clips them with `overflow-y-auto`.
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Fixed head: the scope switch stays above the scroll band in both
          scopes, so switching never moves the control that switched. */}
      <div className="shrink-0 p-2">
        <ScopeSegment
          scope={scope}
          activeCount={state.sessions.length}
          archivedCount={archived.length}
        />
      </div>

      {scope === 'live'
        ? <LiveScopeBody state={state} {...live} />
        : <ArchivedScopeBody onReviewSession={onReviewSession} />}

      {/* Daemon health: collapses to nothing when there is no daemon to watch.
          Both scopes — it describes the daemon, not the list. */}
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
          as a separate control rather than part of the bar.

          Live scope only: `onNew` starts a LIVE session, and offering it beside
          a list of archived ones would start a session the list it sits under
          can never show.

          And only while a session is OPEN. With none open (`newSelected`) the
          dock is already the DispatchBar — the very surface this button exists
          to reveal — so the button would sit directly above a panel that makes
          it redundant. It used to render highlighted in that state, to mark the
          new-session slot as the target; the lit-up DispatchBar beneath it says
          the same thing without a second control. On phone and tablet this is
          every time the column is on screen at all: `app.list` is only shown
          there with no item open, so the button was never useful on a phone. */}
      {scope === 'live' && !newSelected && (
        <div className="shrink-0 p-2">
          <button
            type="button"
            onClick={onNew}
            className={
              'flex w-full min-h-target items-center gap-2 rounded-md border border-edge px-3 py-2 text-sm font-medium text-ink hover:bg-surface-raised ' +
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'
            }
          >
            <Plus size={16} aria-hidden="true" />
            New Session
          </button>
        </div>
      )}
    </div>
  )
}

/** `onNew` and `newSelected` are the frame's, not the body's: the button they
 *  drive sits in the column's foot, below the daemon indicator. */
type LiveScopeBodyProps = Omit<
  SessionListColumnProps,
  'scope' | 'newSelected' | 'onNew' | 'onReviewSession'
>

/** The live scope: the column body as it has always been, unchanged. */
function LiveScopeBody({
  state,
  accounts,
  isLoading,
  selectedId,
  adoptionEnabled = false,
  listMode = false,
  onOpen,
  onRename,
  onClose,
  onDelete,
  onQuickResume,
  onExitList,
}: LiveScopeBodyProps) {
  const { sessions, queued, pendingApproval, search, ordering, nav } = state
  const { filters, setFilters, filterOptions } = state
  // One source of truth for "is anything narrowing the list" — `listFilters`'
  // own predicate, never a re-derived `status !== null || …` here.
  const filtered = hasActiveFilters(filters)

  return (
    <ScopeBody
      controls={
        // Gated on the whole list, pre-spawn rows included: a lone queued
        // session is still a session, and an empty toolbar reads as broken.
        sessions.length > 0 && (
          <SessionListControls
            groupBy={ordering.groupBy}
            sortBy={ordering.sortBy}
            onGroupByChange={ordering.setGroupBy}
            onSortByChange={ordering.setSortBy}
            searchQuery={search.query}
            onSearchChange={search.setQuery}
            filters={{ value: filters, options: filterOptions, onChange: setFilters }}
          />
        )
      }
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
    </ScopeBody>
  )
}
