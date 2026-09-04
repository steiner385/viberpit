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
// `:id` alone is the wrong question — `/archived/:id` is an archived transcript
// and must not attach a live runtime — so the section comes from `navDefForPath`,
// the same map the route table is built from.
import { useLocation, useParams } from 'react-router-dom'
import { navDefForPath } from './navDefs'

/** The open session's id, or null when the route has none. */
export function useOpenSessionId(): string | null {
  const { id } = useParams()
  const { pathname } = useLocation()
  return id && navDefForPath(pathname)?.id === 'sessions' ? id : null
}
