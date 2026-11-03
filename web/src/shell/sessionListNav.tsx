/* eslint-disable react-refresh/only-export-components -- mixed export file: the
   provider and the hook that reads it are two halves of one contract. Same
   convention as `shell/sessionGestures.tsx`. */
/**
 * The Sessions list's ORDER, handed down from the shell to the open session.
 *
 * Alt+[ / Alt+] (the shell's dispatcher) and the session header's ‹ › chevrons
 * must step to the same session. The order that decides it — the one the list
 * renders, filters, search and collapsed groups applied — is
 * `useSessionListState().ordering.orderedSessions`, and the shell holds the only
 * instance of that state. The header is mounted by `PageHost`, below the router
 * outlet, so a prop cannot reach it; this context does.
 *
 * It replaces `useSessionNeighbors`, which re-derived an ordering of its own from
 * the sessions query with no filters and no search — so the chevrons could step
 * to a session the list was hiding, and would have disagreed with the keys.
 */
import * as React from 'react'
import { stepSessionId } from '@/session/listStep'

export interface SessionListNav {
  /** The session one step from `fromId` in the list order (see `stepSessionId`). */
  neighbor: (fromId: string | null, dir: 'prev' | 'next') => string | null
  /** Open `id` the way a click on its list row does (pending ids are ignored). */
  open: (id: string) => void
}

const SessionListNavContext = React.createContext<SessionListNav | null>(null)

export function SessionListNavProvider({
  orderedIds, onOpen, children,
}: {
  orderedIds: readonly string[]
  onOpen: (id: string) => void
  children: React.ReactNode
}) {
  const value = React.useMemo<SessionListNav>(() => ({
    neighbor: (fromId, dir) => stepSessionId(orderedIds, fromId, dir),
    open: onOpen,
  }), [orderedIds, onOpen])
  return <SessionListNavContext.Provider value={value}>{children}</SessionListNavContext.Provider>
}

/** Null outside the shell: there is no list to step through, so no chevrons. */
export function useSessionListNav(): SessionListNav | null {
  return React.useContext(SessionListNavContext)
}
