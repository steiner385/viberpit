import { createContext, useContext } from 'react'
import type { Turn } from '@/session/reducer'
import type { ForkTarget } from '@/session/forkTarget'

/**
 * What the message menu's "Fork from here" (D-8) can do, provided by the LIVE
 * session's `SessionBody` (`ForkProvider`) when its engine can fork. With no
 * provider — the archived reader, the sub-agent page, the assistant drawer, an
 * engine without the capability — the item is not offered.
 *
 * A context for the same reason as `fileRefContext`: the menu sits at the bottom
 * of the shared transcript tree and only one of its hosts can fork.
 */
export interface ForkActions {
  /** `turn`'s fork target, or null when it has none. Reads the transcript at call time. */
  targetFor: (turn: Turn) => ForkTarget | null
  fork: (target: ForkTarget) => Promise<void>
}

export const ForkContext = createContext<ForkActions | null>(null)

export function useForkActions(): ForkActions | null {
  return useContext(ForkContext)
}
