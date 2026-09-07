import type { Params } from 'react-router-dom'
import type { NavId } from './navDefs'

/** What the current route has open, if anything. */
export type OpenItem =
  | { kind: 'session'; id: string }
  | { kind: 'archived'; id: string }

export type OpenItemResolver = (params: Readonly<Params<string>>) => OpenItem | null

/**
 * Per-section "is something open?" from the matched route's params.
 *
 * Sessions: the archived reader's param is `archivedId`, not `id`, and that
 * NAME is what keeps `PageHost` from mounting a live runtime over a dead
 * session (`session/sessionNavState.ts` decides it). A section with no entry
 * here never reports an open item, so a `:id`-shaped route elsewhere cannot
 * be mistaken for a session.
 */
export const OPEN_ITEM_RESOLVERS: Partial<Record<NavId, OpenItemResolver>> = {
  sessions: ({ id, archivedId }) => {
    if (archivedId) return { kind: 'archived', id: archivedId }
    if (id) return { kind: 'session', id }
    return null
  },
}
