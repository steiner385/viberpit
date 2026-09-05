// web/src/shell/openSessionId.ts
//
// "Which session does the current route have open?" — asked in exactly one place.
//
// Two regions need the answer and they must never disagree:
//
//   · `PageHost` uses it to decide whether to mount `SessionRuntimeProvider`.
//   · `PromptDock` uses it to decide between `SessionComposer` (which reads that
//     provider) and `DispatchBar` (which does not).
//
// If those two ever computed it differently, the failure is not cosmetic: the
// dock would render `SessionComposer` with no provider above it and
// `useSessionRuntimeContext()` would throw, taking the whole route down. Making
// it one function is a guarantee; a test that both happen to agree today is not.
//
// `:id` alone is the wrong question — an archived transcript must not attach a
// live runtime. Archived used to be its own nav section, so checking the
// section (via `navDefForPath`, the same map the route table is built from)
// was what excluded it. Since the archived routes moved under `/sessions`
// (`/sessions/archived/:archivedId`), that route resolves to the SAME section
// as a live session, so the section check no longer does the excluding — the
// gate is now the PARAM NAME. The archived route's param is `archivedId`, not
// `id`, so `useParams().id` is simply undefined there; `sessionNavState`'s
// `ARCHIVED_SESSION_ROUTE` is the one place that name is decided, and reusing
// it here would silently attach a live runtime to a dead session.
// The section check stays: it still excludes every OTHER `:id`-shaped detail
// route that isn't Sessions.
import { useLocation, useParams } from 'react-router-dom'
import { navDefForPath } from './navDefs'

/** The open session's id, or null when the route has none. */
export function useOpenSessionId(): string | null {
  const { id } = useParams()
  const { pathname } = useLocation()
  return id && navDefForPath(pathname)?.id === 'sessions' ? id : null
}
