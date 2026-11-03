import { createContext, useContext } from 'react'
import type { Rewind } from './useRewind'

/**
 * What the message menu's "Rewind to here" / "Edit & resend" and the rewind divider's
 * Undo (D-9) can do, provided by the LIVE session's `SessionBody` (`RewindProvider`)
 * when its engine can rewind. With no provider — the archived reader, the sub-agent
 * page, the assistant drawer, an engine without the capability — nothing is offered.
 * A context for the same reason as D-8's `forkContext`.
 */
export interface RewindActions extends Pick<Rewind, 'start' | 'undo'> {
  /** The server would refuse now (`rewindBlocked`): the items show, disabled. */
  busy: boolean
}

export const RewindContext = createContext<RewindActions | null>(null)

export function useRewindActions(): RewindActions | null {
  return useContext(RewindContext)
}
