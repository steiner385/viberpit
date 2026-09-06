// web/src/features/sessions/ArchivedScopeBody.tsx
//
// The archived scope of the sessions list column: its controls strip and its
// scrolling band, and the ONLY place `ArchivedSessionList` is mounted.
//
// It contributes exactly two flex children — a `shrink-0` strip and the one
// scrolling band — so `SessionListColumn`'s layout is identical in either
// scope: fixed head, one scroll region, fixed foot.
//
// Its hook lives HERE rather than in the column so the archived SEARCH and
// ORDERING state belongs to the body that renders the rows. It is NOT what
// scopes the archived query: the column subscribes to `useArchivedSessions()`
// unconditionally for the segment's count, so that query polls in both scopes —
// as it did when the nav badge read it. This adds a subscriber to the same
// cached key, not a second fetch. The live poll is untouched either way; the
// two streams are never merged (D4).
//
// It is a component of its own rather than a private function inside
// `SessionListColumn` because it is the unit the archived tests mount: the
// controls strip and the list are one composition, and a test that rebuilt that
// composition by hand would be a second copy of it.
import { SessionListControls } from '@/components/SessionListControls'
import { ArchivedSessionList } from '@/components/ArchivedSessionList'
import { ARCHIVED_GROUP_OPTIONS, ARCHIVED_SORT_OPTIONS } from '@/session/archivedOrdering'
import { ScopeBody } from './ScopeBody'
import { useArchivedListState } from './useArchivedListState'

export interface ArchivedScopeBodyProps {
  /** Open an archived session's read-only transcript (DEC-6). */
  onReviewSession: (id: string) => void
}

export function ArchivedScopeBody({ onReviewSession }: ArchivedScopeBodyProps) {
  const state = useArchivedListState()
  const { sessions, search, ordering } = state

  return (
    <ScopeBody
      controls={
        // Gated on the whole archived list, exactly as the live strip is gated
        // on the whole live one: an empty toolbar reads as broken.
        sessions.length > 0 && (
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
        )
      }
    >
      <ArchivedSessionList state={state} onReviewSession={onReviewSession} />
    </ScopeBody>
  )
}
