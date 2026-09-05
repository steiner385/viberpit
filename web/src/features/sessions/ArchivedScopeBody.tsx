// web/src/features/sessions/ArchivedScopeBody.tsx
//
// The archived scope of the sessions list column: its controls strip and its
// scrolling band, and the ONLY place `ArchivedSessionList` is mounted.
//
// It contributes exactly two flex children — a `shrink-0` strip and the one
// scrolling band — so `SessionListColumn`'s layout is identical in either
// scope: fixed head, one scroll region, fixed foot.
//
// Its hook lives HERE rather than in the column, so the 30 s archived poll runs
// only while this scope is on screen and never behind the live list. The
// column's own `useArchivedSessions()` (for the segment's count) is the same
// cached query key, so mounting this adds no fetch.
//
// It is a component of its own rather than a private function inside
// `SessionListColumn` because it is the unit the archived tests mount: the
// controls strip and the list are one composition, and a test that rebuilt that
// composition by hand would be a second copy of it.
import { SessionListControls } from '@/components/SessionListControls'
import { ArchivedSessionList } from '@/components/ArchivedSessionList'
import { ARCHIVED_GROUP_OPTIONS, ARCHIVED_SORT_OPTIONS } from '@/session/archivedOrdering'
import { useArchivedListState } from './useArchivedListState'

export interface ArchivedScopeBodyProps {
  /** Open an archived session's read-only transcript (DEC-6). */
  onReviewSession: (id: string) => void
}

export function ArchivedScopeBody({ onReviewSession }: ArchivedScopeBodyProps) {
  const state = useArchivedListState()
  const { sessions, search, ordering } = state

  return (
    <>
      {/* Gated on the whole archived list, exactly as the live strip is gated on
          the whole live one: an empty toolbar reads as broken, and `empty:hidden`
          keeps a session-less list from painting a bare strip. */}
      <div className="shrink-0 space-y-2 px-2 pb-2 empty:hidden">
        {sessions.length > 0 && (
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
      </div>

      {/* Same testid as the live band: one scroll owner per column, whichever
          scope is showing. */}
      <div
        data-testid="session-list-scroll"
        className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2"
      >
        <ArchivedSessionList state={state} onReviewSession={onReviewSession} />
      </div>
    </>
  )
}
