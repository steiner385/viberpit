// web/src/features/sessions/useArchivedListState.ts
//
// The archived scope's state, mirroring `useSessionListState` for the live one.
//
// It is deliberately NOT merged with that hook, and the two queries are never
// merged either: the live list polls every 3–5s and is reconciled over the
// WebSocket, while the archived list refetches every 30s and can hold hundreds
// of rows (there is a purge-the-oldest-200 button). Folding archived rows into
// the live stream would drag all of them through the hot poll for nothing, and
// `ArchivedSession` is a distinct type on purpose — widening `SessionStatus`
// with an `'archived'` member is what the fleet mirror-zombie guard (#1033,
// #898) depends on NOT happening.
//
// It is also thinner on purpose. There is no `nav` cursor: the prompt bar drives
// the LIVE list, and an archived row has nothing to dispatch to. There are no
// Status/Project/Environment filters: `ArchivedSession` carries no status, host
// or PR to filter on.
import { useArchivedSessions } from '@/api/hooks'
import type { ArchivedSession } from '@/api/types'
import { useSessionSearch, type SessionSearch } from '@/session/useSessionSearch'
import { useItemOrdering, type OrderingHandle } from '@/session/useSessionOrdering'
import { ARCHIVED_SPEC, type ArchGroupBy, type ArchSortBy } from '@/session/archivedOrdering'

export type ArchivedOrdering = OrderingHandle<ArchGroupBy, ArchSortBy, ArchivedSession>

export interface ArchivedListState {
  /** Every archived session, unfiltered — what the controls strip is gated on. */
  sessions: readonly ArchivedSession[]
  search: SessionSearch
  ordering: ArchivedOrdering
}

export function useArchivedListState(): ArchivedListState {
  const { data: sessions = [] } = useArchivedSessions()
  const search = useSessionSearch('archived')
  const ordering = useItemOrdering(sessions, ARCHIVED_SPEC, search.matchedIds)
  return { sessions, search, ordering }
}
