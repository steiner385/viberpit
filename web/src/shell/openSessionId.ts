// web/src/shell/openSessionId.ts
//
// "What does the current route have open?" — asked in exactly one place.
//
// Four consumers need the answer and they must never disagree:
//
//   · `PageHost` uses it to decide whether to mount `SessionRuntimeProvider`.
//   · `PromptDock` uses it to decide between `SessionComposer` (which reads that
//     provider) and `DispatchBar` (which does not).
//   · `AppShell` uses it to decide whether SOMETHING is open, for the region
//     matrix's `itemOpen` — a phone gives its one content band to either the
//     list or the page.
//   · `AssistantSheet` uses `useOpenSessionId` to include the open session in
//     the assistant's context chip.
//
// If the first two ever computed it differently, the failure is not cosmetic:
// the dock would render `SessionComposer` with no provider above it and
// `useSessionRuntimeContext()` would throw, taking the whole route down. If the
// third disagreed, a phone would show the list on top of a page that has
// nothing behind it to fall back to. Making it one function is a guarantee; a
// test that both happen to agree today is not.
//
// `:id` alone is the wrong question for any of the three — an archived
// transcript must not attach a live runtime, but it IS something the phone must
// show instead of the list. Archived used to be its own nav section, so
// checking the section (via `navDefForPath`, the same map the route table is
// built from) was what excluded it from a live runtime. Since the archived
// routes moved under `/sessions` (`/sessions/archived/:archivedId`), that route
// resolves to the SAME section as a live session, so the section check no
// longer does the excluding — the gate is now the PARAM NAME. The archived
// route's param is `archivedId`, not `id`, so `useParams().id` is simply
// undefined there; `sessionNavState`'s `ARCHIVED_SESSION_ROUTE` is the one place
// that name is decided, and reusing it here would silently attach a live
// runtime to a dead session.
// The section check stays: it still excludes every OTHER `:id`-shaped detail
// route that isn't Sessions.
import { useLocation, useParams } from 'react-router-dom'
import { navDefForPath } from './navDefs'
import { OPEN_ITEM_RESOLVERS } from './openItems'
import type { OpenItem } from './openItems'

export type { OpenItem }

/**
 * The open item, or null. See the module docstring for why one function must
 * answer for all three consumers. The per-section rule lives in
 * `openItems.ts`; this only picks the section from the path.
 */
export function useOpenItem(): OpenItem | null {
  const params = useParams()
  const { pathname } = useLocation()
  const def = navDefForPath(pathname)
  if (!def) return null
  return OPEN_ITEM_RESOLVERS[def.id]?.(params) ?? null
}

/** The open LIVE session's id, or null — unchanged signature/behaviour, now
 *  re-derived from `useOpenItem` rather than a parallel computation. */
export function useOpenSessionId(): string | null {
  const item = useOpenItem()
  return item?.kind === 'session' ? item.id : null
}
