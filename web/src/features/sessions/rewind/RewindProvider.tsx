import { useMemo, type ReactNode } from 'react'
import { rewindBlocked } from '@/session/rewindTarget'
import { RewindContext, type RewindActions } from './rewindContext'
import { useRewind } from './useRewind'

/** Provides `RewindContext` for one live session (D-9) and hosts its confirm dialog.
 *  `enabled` is the engine's `rewind` capability: off, no value, so no rewind items.
 *  `status` is the live status: the items are disabled where the server would refuse
 *  (`rewindBlocked`). `onExpired` drops a divider whose undo the server called expired. */
export function RewindProvider({ sessionId, enabled, status, onExpired, children }: {
  sessionId: string; enabled: boolean; status: string; onExpired?: (rewindId: string) => void; children: ReactNode
}) {
  const { start, undo, element } = useRewind(sessionId, { onExpired })
  const busy = rewindBlocked(status)
  const value = useMemo<RewindActions | null>(
    () => (enabled ? { start, undo, busy } : null), [enabled, start, undo, busy])
  return <RewindContext.Provider value={value}>{children}{element}</RewindContext.Provider>
}
